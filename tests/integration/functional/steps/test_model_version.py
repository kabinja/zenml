#  Copyright (c) ZenML GmbH 2023. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at:
#
#       https://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
#  or implied. See the License for the specific language governing
#  permissions and limitations under the License.

from typing import Tuple
from unittest import mock
from uuid import uuid4

import pytest
from typing_extensions import Annotated

from zenml import get_pipeline_context, get_step_context, pipeline, step
from zenml.artifacts.artifact_config import ArtifactConfig
from zenml.artifacts.external_artifact import ExternalArtifact
from zenml.client import Client
from zenml.enums import ModelStages
from zenml.model.model import Model


@step
def _assert_that_model_set(name="foo"):
    """Step asserting that passed model name and version is in model context."""
    assert get_step_context().model.name == name
    assert get_step_context().model.version == "1"


def test_model_passed_to_step_context_via_step(clean_client: "Client"):
    """Test that model version was passed to step context via step."""

    @pipeline(name="bar", enable_cache=False)
    def _simple_step_pipeline():
        _assert_that_model_set.with_options(
            model=Model(name="foo"),
        )()

    _simple_step_pipeline()


def test_model_passed_to_step_context_via_pipeline(
    clean_client: "Client",
):
    """Test that model version was passed to step context via pipeline."""

    @pipeline(
        name="bar",
        model=Model(name="foo"),
        enable_cache=False,
    )
    def _simple_step_pipeline():
        _assert_that_model_set()

    _simple_step_pipeline()


def test_model_passed_to_step_context_via_step_and_pipeline(
    clean_client: "Client",
):
    """Test that model version was passed to step context via both, but step is dominating."""

    @pipeline(
        name="bar",
        model=Model(name="bar"),
        enable_cache=False,
    )
    def _simple_step_pipeline():
        _assert_that_model_set.with_options(
            model=Model(name="foo"),
        )()

    _simple_step_pipeline()


def test_model_passed_to_step_context_and_switches(
    clean_client: "Client",
):
    """Test that model version was passed to step context via both and switches possible."""

    @pipeline(
        name="bar",
        model=Model(name="bar"),
        enable_cache=False,
    )
    def _simple_step_pipeline():
        # this step will use Model from itself
        _assert_that_model_set.with_options(
            model=Model(name="foo"),
        )()
        # this step will use Model from pipeline
        _assert_that_model_set(name="bar")
        # and another switch of context
        _assert_that_model_set.with_options(
            model=Model(name="foobar"),
        )(name="foobar")

    _simple_step_pipeline()


@step(model=Model(name="foo"))
def _this_step_creates_a_version():
    return 1


@step
def _this_step_does_not_create_a_version():
    return 1


def test_create_new_versions_both_pipeline_and_step(clean_client: "Client"):
    """Test that model version on step and pipeline levels can create new model versions at the same time."""
    desc = "Should be the best version ever!"

    @pipeline(
        name="bar",
        model=Model(name="bar", description=desc),
        enable_cache=False,
    )
    def _this_pipeline_creates_a_version():
        _this_step_creates_a_version()
        _this_step_does_not_create_a_version()

    _this_pipeline_creates_a_version()

    foo = clean_client.get_model("foo")
    assert foo.name == "foo"
    foo_version = clean_client.get_model_version("foo", ModelStages.LATEST)
    assert foo_version.number == 1

    bar = clean_client.get_model("bar")
    assert bar.name == "bar"
    bar_version = clean_client.get_model_version("bar", ModelStages.LATEST)
    assert bar_version.number == 1
    assert bar_version.description == desc

    _this_pipeline_creates_a_version()

    foo_version = clean_client.get_model_version("foo", ModelStages.LATEST)
    assert foo_version.number == 2

    bar_version = clean_client.get_model_version("bar", ModelStages.LATEST)
    assert foo_version.number == 2
    assert bar_version.description == desc


def test_create_new_version_only_in_step(clean_client: "Client"):
    """Test that model version on step level only can create new model version."""

    @pipeline(name="bar", enable_cache=False)
    def _this_pipeline_does_not_create_a_version():
        _this_step_creates_a_version()
        _this_step_does_not_create_a_version()

    _this_pipeline_does_not_create_a_version()

    bar = clean_client.get_model("foo")
    assert bar.name == "foo"
    bar_version = clean_client.get_model_version("foo", ModelStages.LATEST)
    assert bar_version.number == 1

    _this_pipeline_does_not_create_a_version()

    bar_version = clean_client.get_model_version("foo", ModelStages.LATEST)
    assert bar_version.number == 2


def test_create_new_version_only_in_pipeline(clean_client: "Client"):
    """Test that model version on pipeline level only can create new model version."""

    @pipeline(
        name="bar",
        model=Model(name="bar"),
        enable_cache=False,
    )
    def _this_pipeline_creates_a_version():
        _this_step_does_not_create_a_version()

    _this_pipeline_creates_a_version()

    foo = clean_client.get_model("bar")
    assert foo.name == "bar"
    foo_version = clean_client.get_model_version("bar", ModelStages.LATEST)
    assert foo_version.number == 1

    _this_pipeline_creates_a_version()

    foo_version = clean_client.get_model_version("bar", ModelStages.LATEST)
    assert foo_version.number == 2


@step
def _this_step_produces_output() -> Annotated[int, "data"]:
    return 1


@step
def _this_step_tries_to_recover(run_number: int):
    mv = get_step_context().model._get_or_create_model_version()
    assert (
        len(mv.data_artifact_ids["data"]) == run_number
    ), "expected AssertionError"

    raise Exception("make pipeline fail")


@pytest.mark.parametrize(
    "model",
    [
        Model(
            name="foo",
        ),
        Model(
            name="foo",
            version="test running version",
        ),
    ],
    ids=["default_running_name", "custom_running_name"],
)
def test_recovery_of_steps(clean_client: "Client", model: Model):
    """Test that model version can recover states after previous fails."""

    @pipeline(
        name="bar",
        enable_cache=False,
    )
    def _this_pipeline_will_recover(run_number: int):
        _this_step_produces_output()
        _this_step_tries_to_recover(
            run_number, after=["_this_step_produces_output"]
        )

    with pytest.raises(Exception, match="make pipeline fail"):
        _this_pipeline_will_recover.with_options(model=model)(1)
    if model.version is None:
        model.version = "1"
    with pytest.raises(Exception, match="make pipeline fail"):
        _this_pipeline_will_recover.with_options(model=model)(2)
    with pytest.raises(Exception, match="make pipeline fail"):
        _this_pipeline_will_recover.with_options(model=model)(3)

    mv = clean_client.get_model_version(
        model_name_or_id="foo",
        model_version_name_or_number_or_id=model.version,
    )
    assert mv.name == model.version
    assert len(mv.data_artifact_ids) == 1
    assert len(mv.data_artifact_ids["data"]) == 3


@step(model=Model(name="foo"))
def _new_version_step():
    return 1


@step
def _no_model_step():
    return 1


@pipeline(
    enable_cache=False,
    model=Model(name="foo"),
)
def _new_version_pipeline_overridden_warns():
    _new_version_step()


@pipeline(
    enable_cache=False,
    model=Model(name="foo"),
)
def _new_version_pipeline_not_warns():
    _no_model_step()


@pipeline(enable_cache=False)
def _no_new_version_pipeline_not_warns():
    _new_version_step()


@pipeline(enable_cache=False)
def _no_new_version_pipeline_warns_on_steps():
    _new_version_step()
    _new_version_step()


@pipeline(
    enable_cache=False,
    model=Model(name="foo"),
)
def _new_version_pipeline_warns_on_steps():
    _new_version_step()
    _no_model_step()


@pytest.mark.parametrize(
    "pipeline, expected_warning",
    [
        (
            _new_version_pipeline_overridden_warns,
            "is overridden in all steps",
        ),
        (_new_version_pipeline_not_warns, ""),
        (_no_new_version_pipeline_not_warns, ""),
        (
            _no_new_version_pipeline_warns_on_steps,
            "is configured only in one place of the pipeline",
        ),
        (
            _new_version_pipeline_warns_on_steps,
            "is configured only in one place of the pipeline",
        ),
    ],
    ids=[
        "Pipeline with one step, which overrides model - warns that pipeline conf is useless.",
        "Configuration in pipeline only - not warns.",
        "Configuration in step only - not warns.",
        "Two steps ask to create new versions - warning to keep it in one place.",
        "Pipeline and one of the steps ask to create new versions - warning to keep it in one place.",
    ],
)
def test_multiple_definitions_create_new_version_warns(
    clean_client: "Client", pipeline, expected_warning
):
    """Test that setting conflicting model versions are raise warnings to user."""
    with mock.patch("zenml.new.pipelines.pipeline.logger.warning") as logger:
        pipeline()
        if expected_warning:
            logger.assert_called_once()
            assert expected_warning in logger.call_args[0][0]
        else:
            logger.assert_not_called()


@pipeline(name="bar", enable_cache=False)
def _pipeline_run_link_attached_from_pipeline_context_single_step():
    _this_step_produces_output()


@pipeline(name="bar", enable_cache=False)
def _pipeline_run_link_attached_from_pipeline_context_multiple_steps():
    _this_step_produces_output()
    _this_step_produces_output()


@pytest.mark.parametrize(
    "pipeline",
    (
        _pipeline_run_link_attached_from_pipeline_context_single_step,
        _pipeline_run_link_attached_from_pipeline_context_multiple_steps,
    ),
    ids=["Single step pipeline", "Multiple steps pipeline"],
)
def test_pipeline_run_link_attached_from_pipeline_context(
    clean_client: "Client", pipeline
):
    """Tests that current pipeline run information is attached to model version by pipeline context."""
    run_name_1 = f"bar_run_{uuid4()}"
    pipeline.with_options(
        run_name=run_name_1,
        model=Model(
            name="foo",
        ),
    )()
    run_name_2 = f"bar_run_{uuid4()}"
    pipeline.with_options(
        run_name=run_name_2,
        model=Model(name="foo", version=ModelStages.LATEST),
    )()

    mv = clean_client.get_model_version(
        model_name_or_id="foo",
        model_version_name_or_number_or_id=ModelStages.LATEST,
    )

    assert len(mv.pipeline_run_ids) == 2
    assert {run_name for run_name in mv.pipeline_run_ids} == {
        run_name_1,
        run_name_2,
    }


@pipeline(name="bar", enable_cache=False)
def _pipeline_run_link_attached_from_step_context_single_step(
    mv: Model,
):
    _this_step_produces_output.with_options(model=mv)()


@pipeline(name="bar", enable_cache=False)
def _pipeline_run_link_attached_from_step_context_multiple_step(
    mv: Model,
):
    _this_step_produces_output.with_options(model=mv)()
    _this_step_produces_output.with_options(model=mv)()


@pytest.mark.parametrize(
    "pipeline",
    (
        _pipeline_run_link_attached_from_step_context_single_step,
        _pipeline_run_link_attached_from_step_context_multiple_step,
    ),
    ids=["Single step pipeline", "Multiple steps pipeline"],
)
def test_pipeline_run_link_attached_from_step_context(
    clean_client: "Client", pipeline
):
    """Tests that current pipeline run information is attached to model version by step context."""
    run_name_1 = f"bar_run_{uuid4()}"
    pipeline.with_options(
        run_name=run_name_1,
    )(
        Model(
            name="foo",
        )
    )
    run_name_2 = f"bar_run_{uuid4()}"
    pipeline.with_options(
        run_name=run_name_2,
    )(Model(name="foo", version=ModelStages.LATEST))

    mv = clean_client.get_model_version(
        model_name_or_id="foo",
        model_version_name_or_number_or_id=ModelStages.LATEST,
    )

    assert len(mv.pipeline_run_ids) == 2
    assert {run_name for run_name in mv.pipeline_run_ids} == {
        run_name_1,
        run_name_2,
    }


@step
def _this_step_has_model_on_artifact_level() -> (
    Tuple[
        Annotated[
            int,
            "declarative_link",
            ArtifactConfig(
                model_name="declarative", model_version=ModelStages.LATEST
            ),
        ],
        Annotated[
            int,
            "functional_link",
            ArtifactConfig(
                model_name="functional", model_version=ModelStages.LATEST
            ),
        ],
    ]
):
    return 1, 2


@pipeline(enable_cache=False)
def _pipeline_run_link_attached_from_artifact_context_single_step():
    _this_step_has_model_on_artifact_level()


@pipeline(enable_cache=False)
def _pipeline_run_link_attached_from_artifact_context_multiple_step():
    _this_step_has_model_on_artifact_level()
    _this_step_has_model_on_artifact_level()


@pipeline(
    enable_cache=False,
    model=Model(name="pipeline", version=ModelStages.LATEST),
)
def _pipeline_run_link_attached_from_mixed_context_single_step():
    _this_step_has_model_on_artifact_level()
    _this_step_produces_output()
    _this_step_produces_output.with_options(
        model=Model(name="step", version=ModelStages.LATEST),
    )()


@pipeline(
    enable_cache=False,
    model=Model(name="pipeline", version=ModelStages.LATEST),
)
def _pipeline_run_link_attached_from_mixed_context_multiple_step():
    _this_step_has_model_on_artifact_level()
    _this_step_produces_output()
    _this_step_produces_output.with_options(
        model=Model(name="step", version=ModelStages.LATEST),
    )()
    _this_step_has_model_on_artifact_level()
    _this_step_produces_output()
    _this_step_produces_output.with_options(
        model=Model(name="step", version=ModelStages.LATEST),
    )()


@pytest.mark.parametrize(
    "pipeline,model_names",
    (
        (
            _pipeline_run_link_attached_from_artifact_context_single_step,
            ["declarative", "functional"],
        ),
        (
            _pipeline_run_link_attached_from_artifact_context_multiple_step,
            ["declarative", "functional"],
        ),
        (
            _pipeline_run_link_attached_from_mixed_context_single_step,
            ["declarative", "functional", "step", "pipeline"],
        ),
        (
            _pipeline_run_link_attached_from_mixed_context_multiple_step,
            ["declarative", "functional", "step", "pipeline"],
        ),
    ),
    ids=[
        "Single step pipeline (declarative+functional)",
        "Multiple steps pipeline (declarative+functional)",
        "Single step pipeline (declarative+functional+step+pipeline)",
        "Multiple steps pipeline (declarative+functional+step+pipeline)",
    ],
)
def test_pipeline_run_link_attached_from_mixed_context(
    pipeline, model_names, clean_client: "Client"
):
    """Tests that current pipeline run information is attached to model version by artifact context.

    Here we use 2 models and Artifacts has different configs to link there.
    """
    models = []
    for model_name in model_names:
        models.append(
            clean_client.create_model(
                name=model_name,
            )
        )
        clean_client.create_model_version(
            model_name_or_id=model_name,
            name="good_one",
        )

    run_name_1 = f"bar_run_{uuid4()}"
    pipeline.with_options(
        run_name=run_name_1,
    )()
    run_name_2 = f"bar_run_{uuid4()}"
    pipeline.with_options(
        run_name=run_name_2,
    )()

    for model in models:
        mv = clean_client.get_model_version(
            model_name_or_id=model.id,
            model_version_name_or_number_or_id=ModelStages.LATEST,
        )

        assert len(mv.pipeline_run_ids) == 2
        assert {run_name for run_name in mv.pipeline_run_ids} == {
            run_name_1,
            run_name_2,
        }


@step
def _consumer_step(a: int, b: int):
    assert a == b


@step(model=Model(name="step"))
def _producer_step() -> (
    Tuple[
        Annotated[int, "output_0"],
        Annotated[int, "output_1"],
        Annotated[int, "output_2"],
    ]
):
    return 1, 2, 3


@pipeline
def _consumer_pipeline_with_step_context():
    _consumer_step.with_options(
        model=Model(name="step", version=ModelStages.LATEST)
    )(ExternalArtifact(name="output_0"), 1)


@pipeline(model=Model(name="step", version=ModelStages.LATEST))
def _consumer_pipeline_with_pipeline_context():
    _consumer_step(
        ExternalArtifact(name="output_2"),
        3,
    )


@pipeline
def _producer_pipeline():
    _producer_step()


def test_that_consumption_also_registers_run_in_model(
    clean_client: "Client",
):
    """Test that consumption scenario also registers run in model version."""
    producer_run = f"producer_run_{uuid4()}"
    consumer_run_1 = f"consumer_run_1_{uuid4()}"
    consumer_run_2 = f"consumer_run_2_{uuid4()}"
    _producer_pipeline.with_options(
        run_name=producer_run, enable_cache=False
    )()
    _consumer_pipeline_with_step_context.with_options(
        run_name=consumer_run_1
    )()
    _consumer_pipeline_with_pipeline_context.with_options(
        run_name=consumer_run_2
    )()

    mv = clean_client.get_model_version(
        model_name_or_id="step",
        model_version_name_or_number_or_id=ModelStages.LATEST,
    )

    assert len(mv.pipeline_run_ids) == 3
    assert {run_name for run_name in mv.pipeline_run_ids} == {
        producer_run,
        consumer_run_1,
        consumer_run_2,
    }


def test_that_if_some_steps_request_new_version_but_cached_new_version_is_still_created(
    clean_client: "Client",
):
    """Test that if one of the steps requests a new version but was cached a new version is still created for other steps."""

    @pipeline(model=Model(name="step", version=ModelStages.LATEST))
    def _inner_pipeline():
        # this step requests a new version, but can be cached
        _this_step_produces_output.with_options(model=Model(name="step"))()
        # this is an always run step
        _this_step_produces_output.with_options(enable_cache=False)()

    # this will run all steps, including one requesting new version
    run_1 = f"run_{uuid4()}"
    _inner_pipeline.with_options(run_name=run_1)()
    # here the step requesting new version is cached
    run_2 = f"run_{uuid4()}"
    _inner_pipeline.with_options(run_name=run_2)()

    model = clean_client.get_model(model_name_or_id="step")
    mvs = model.versions
    assert len(mvs) == 2
    for mv, run_name in zip(mvs, (run_1, run_2)):
        assert (
            len(
                clean_client.zen_store.get_model_version(
                    mv.id
                ).pipeline_run_ids
            )
            == 1
        )
        assert clean_client.zen_store.get_model_version(
            mv.id
        ).pipeline_run_ids[run_name]


def test_that_pipeline_run_is_removed_on_deletion_of_pipeline_run(
    clean_client: "Client",
):
    """Test that if pipeline run gets deleted - it is removed from model version."""

    @pipeline(
        model=Model(name="step", version=ModelStages.LATEST),
        enable_cache=False,
    )
    def _inner_pipeline():
        _this_step_produces_output.with_options(model=Model(name="step"))()

    run_1 = f"run_{uuid4()}"
    _inner_pipeline.with_options(run_name=run_1)()

    clean_client.delete_pipeline_run(run_1)
    model = clean_client.get_model(model_name_or_id="step")
    mvs = model.versions
    assert len(mvs) == 1
    assert (
        len(
            clean_client.zen_store.get_model_version(
                mvs[0].id
            ).pipeline_run_ids
        )
        == 0
    )


def test_that_pipeline_run_is_removed_on_deletion_of_pipeline(
    clean_client: "Client",
):
    """Test that if pipeline gets deleted - runs are removed from model version."""

    @pipeline(
        model=Model(name="step", version=ModelStages.LATEST),
        enable_cache=False,
        name="test_that_pipeline_run_is_removed_on_deletion_of_pipeline",
    )
    def _inner_pipeline():
        _this_step_produces_output.with_options(model=Model(name="step"))()

    run_1 = f"run_{uuid4()}"
    _inner_pipeline.with_options(run_name=run_1)()

    pipeline_response = clean_client.get_pipeline(
        "test_that_pipeline_run_is_removed_on_deletion_of_pipeline"
    )
    for run in pipeline_response.runs:
        clean_client.delete_pipeline_run(run.id)
    model = clean_client.get_model(model_name_or_id="step")
    mvs = model.versions
    assert len(mvs) == 1
    assert (
        len(
            clean_client.zen_store.get_model_version(
                mvs[0].id
            ).pipeline_run_ids
        )
        == 0
    )


def test_that_artifact_is_removed_on_deletion(
    clean_client: "Client",
):
    """Test that if artifact gets deleted - it is removed from model version."""

    @pipeline(
        model=Model(name="step", version=ModelStages.LATEST),
        enable_cache=False,
    )
    def _inner_pipeline():
        _this_step_produces_output.with_options(model=Model(name="step"))()

    run_1 = f"run_{uuid4()}"
    _inner_pipeline.with_options(run_name=run_1)()

    run = clean_client.get_pipeline_run(run_1)
    pipeline_id = run.pipeline.id
    artifact_version_id = (
        run.steps["_this_step_produces_output"].outputs["data"].id
    )
    clean_client.delete_pipeline(pipeline_id)
    clean_client.delete_pipeline_run(run.id)
    clean_client.delete_artifact_version(artifact_version_id)
    model = clean_client.get_model(model_name_or_id="step")
    mvs = model.versions
    assert len(mvs) == 1
    assert (
        len(
            clean_client.zen_store.get_model_version(
                mvs[0].id
            ).pipeline_run_ids
        )
        == 0
    )


@step
def _this_step_asserts_context_with_artifact(artifact: str):
    """Assert given arg with model number."""
    assert artifact == str(get_step_context().model.id)


@step
def _this_step_produces_output_model() -> (
    Annotated[str, ArtifactConfig(name="artifact")]
):
    """This step produces artifact with model number."""
    return str(get_step_context().model.id)


def test_pipeline_context_pass_artifact_from_model_and_link_run(
    clean_client: "Client",
):
    """Test that ExternalArtifact from pipeline context is matched to proper version and run is linked."""

    @pipeline(model=Model(name="pipeline"), enable_cache=False)
    def _producer(do_promote: bool):
        _this_step_produces_output_model()
        if do_promote:
            get_pipeline_context().model.set_stage(ModelStages.PRODUCTION)

    @pipeline(
        model=Model(name="pipeline", version=ModelStages.PRODUCTION),
        enable_cache=False,
    )
    def _consumer():
        artifact = get_pipeline_context().model.get_artifact("artifact")
        _this_step_asserts_context_with_artifact(artifact)

    _producer.with_options(run_name="run_1")(True)
    _producer.with_options(run_name="run_2")(False)
    _consumer.with_options(run_name="run_3")()

    mv = clean_client.get_model_version(
        model_name_or_id="pipeline",
        model_version_name_or_number_or_id=ModelStages.LATEST,
    )

    assert len(mv.pipeline_run_ids) == 1
    assert {run_name for run_name in mv.pipeline_run_ids} == {"run_2"}

    mv = clean_client.get_model_version(
        model_name_or_id="pipeline",
        model_version_name_or_number_or_id=ModelStages.PRODUCTION,
    )

    assert len(mv.pipeline_run_ids) == 2
    assert {run_name for run_name in mv.pipeline_run_ids} == {"run_1", "run_3"}
