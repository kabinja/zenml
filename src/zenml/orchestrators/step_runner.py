#  Copyright (c) ZenML GmbH 2022. All Rights Reserved.
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

"""Class to run steps."""

import copy
import inspect
from contextlib import nullcontext
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
    Type,
)
from uuid import UUID

from pydantic.typing import get_origin, is_union

from zenml.artifacts.unmaterialized_artifact import UnmaterializedArtifact
from zenml.artifacts.utils import save_artifact
from zenml.client import Client
from zenml.config.step_configurations import StepConfiguration
from zenml.config.step_run_info import StepRunInfo
from zenml.constants import (
    ENV_ZENML_DISABLE_STEP_LOGS_STORAGE,
    handle_bool_env_var,
)
from zenml.exceptions import StepContextError, StepInterfaceError
from zenml.logger import get_logger
from zenml.logging.step_logging import StepLogsStorageContext, redirected
from zenml.materializers.base_materializer import BaseMaterializer
from zenml.model.utils import link_step_artifacts_to_model
from zenml.new.steps.step_context import StepContext, get_step_context
from zenml.orchestrators.publish_utils import (
    publish_step_run_metadata,
    publish_successful_step_run,
)
from zenml.orchestrators.utils import is_setting_enabled
from zenml.steps.step_environment import StepEnvironment
from zenml.steps.utils import (
    OutputSignature,
    parse_return_type_annotations,
    resolve_type_annotation,
)
from zenml.utils import materializer_utils, source_utils

if TYPE_CHECKING:
    from zenml.artifacts.external_artifact_config import (
        ExternalArtifactConfiguration,
    )
    from zenml.config.source import Source
    from zenml.config.step_configurations import Step
    from zenml.models import (
        ArtifactVersionResponse,
        PipelineRunResponse,
        StepRunResponse,
    )
    from zenml.stack import Stack
    from zenml.steps import BaseStep


logger = get_logger(__name__)


class StepRunner:
    """Class to run steps."""

    def __init__(self, step: "Step", stack: "Stack"):
        """Initializes the step runner.

        Args:
            step: The step to run.
            stack: The stack on which the step should run.
        """
        self._step = step
        self._stack = stack

    @property
    def configuration(self) -> StepConfiguration:
        """Configuration of the step to run.

        Returns:
            The step configuration.
        """
        return self._step.config

    def run(
        self,
        pipeline_run: "PipelineRunResponse",
        step_run: "StepRunResponse",
        input_artifacts: Dict[str, "ArtifactVersionResponse"],
        output_artifact_uris: Dict[str, str],
        step_run_info: StepRunInfo,
    ) -> None:
        """Runs the step.

        Args:
            pipeline_run: The model of the current pipeline run.
            step_run: The model of the current step run.
            input_artifacts: The input artifact versions of the step.
            output_artifact_uris: The URIs of the output artifacts of the step.
            step_run_info: The step run info.

        Raises:
            BaseException: A general exception if the step fails.
        """
        if handle_bool_env_var(ENV_ZENML_DISABLE_STEP_LOGS_STORAGE, False):
            step_logging_enabled = False
        else:
            enabled_on_step = step_run.config.enable_step_logs
            enabled_on_pipeline = pipeline_run.config.enable_step_logs

            step_logging_enabled = is_setting_enabled(
                is_enabled_on_step=enabled_on_step,
                is_enabled_on_pipeline=enabled_on_pipeline,
            )

        logs_context = nullcontext()
        if step_logging_enabled and not redirected.get():
            if step_run.logs:
                logs_context = StepLogsStorageContext(  # type: ignore[assignment]
                    logs_uri=step_run.logs.uri
                )
            else:
                logger.debug(
                    "There is no LogsResponseModel prepared for the step. The"
                    "step logging storage is disabled."
                )

        with logs_context:
            step_instance = self._load_step()
            output_materializers = self._load_output_materializers()
            spec = inspect.getfullargspec(
                inspect.unwrap(step_instance.entrypoint)
            )

            # (Deprecated) Wrap the execution of the step function in a step
            # environment that the step function code can access to retrieve
            # information about the pipeline runtime, such as the current step
            # name and the current pipeline run ID
            cache_enabled = is_setting_enabled(
                is_enabled_on_step=step_run_info.config.enable_cache,
                is_enabled_on_pipeline=step_run_info.pipeline.enable_cache,
            )
            output_annotations = parse_return_type_annotations(
                func=step_instance.entrypoint
            )
            with StepEnvironment(
                step_run_info=step_run_info,
                cache_enabled=cache_enabled,
            ):
                self._stack.prepare_step_run(info=step_run_info)

                # Initialize the step context singleton
                StepContext._clear()
                StepContext(
                    pipeline_run=pipeline_run,
                    step_run=step_run,
                    output_materializers=output_materializers,
                    output_artifact_uris=output_artifact_uris,
                    step_run_info=step_run_info,
                    cache_enabled=cache_enabled,
                    output_artifact_configs={
                        k: v.artifact_config
                        for k, v in output_annotations.items()
                    },
                )
                # Prepare Model Context
                self._prepare_model_context_for_step()

                # Parse the inputs for the entrypoint function.
                function_params = self._parse_inputs(
                    args=spec.args,
                    annotations=spec.annotations,
                    input_artifacts=input_artifacts,
                )

                self._link_pipeline_run_to_model_from_context(
                    pipeline_run=pipeline_run
                )

                step_failed = False
                try:
                    return_values = step_instance.call_entrypoint(
                        **function_params
                    )
                except BaseException as step_exception:  # noqa: E722
                    step_failed = True
                    failure_hook_source = (
                        self.configuration.failure_hook_source
                    )
                    if failure_hook_source:
                        logger.info("Detected failure hook. Running...")
                        self.load_and_run_hook(
                            failure_hook_source,
                            step_exception=step_exception,
                        )
                    raise
                finally:
                    step_run_metadata = self._stack.get_step_run_metadata(
                        info=step_run_info,
                    )
                    publish_step_run_metadata(
                        step_run_id=step_run_info.step_run_id,
                        step_run_metadata=step_run_metadata,
                    )
                    self._stack.cleanup_step_run(
                        info=step_run_info, step_failed=step_failed
                    )
                    if not step_failed:
                        success_hook_source = (
                            self.configuration.success_hook_source
                        )
                        if success_hook_source:
                            logger.info("Detected success hook. Running...")
                            self.load_and_run_hook(
                                success_hook_source,
                                step_exception=None,
                            )

                        # Store and publish the output artifacts of the step function.
                        output_data = self._validate_outputs(
                            return_values, output_annotations
                        )
                        artifact_metadata_enabled = is_setting_enabled(
                            is_enabled_on_step=step_run_info.config.enable_artifact_metadata,
                            is_enabled_on_pipeline=step_run_info.pipeline.enable_artifact_metadata,
                        )
                        artifact_visualization_enabled = is_setting_enabled(
                            is_enabled_on_step=step_run_info.config.enable_artifact_visualization,
                            is_enabled_on_pipeline=step_run_info.pipeline.enable_artifact_visualization,
                        )
                        output_artifact_ids = self._store_output_artifacts(
                            output_data=output_data,
                            output_artifact_uris=output_artifact_uris,
                            output_materializers=output_materializers,
                            output_annotations=output_annotations,
                            artifact_metadata_enabled=artifact_metadata_enabled,
                            artifact_visualization_enabled=artifact_visualization_enabled,
                        )
                        link_step_artifacts_to_model(
                            artifact_version_ids=output_artifact_ids
                        )
                        self._link_pipeline_run_to_model_from_artifacts(
                            pipeline_run=pipeline_run,
                            artifact_names=list(output_artifact_ids.keys()),
                            external_artifacts=list(
                                step_run.config.external_input_artifacts.values()
                            ),
                        )
                    StepContext._clear()  # Remove the step context singleton

            # Update the status and output artifacts of the step run.
            publish_successful_step_run(
                step_run_id=step_run_info.step_run_id,
                output_artifact_ids=output_artifact_ids,
            )

    def _load_step(self) -> "BaseStep":
        """Load the step instance.

        Returns:
            The step instance.
        """
        from zenml.steps import BaseStep

        step_instance = BaseStep.load_from_source(self._step.spec.source)
        step_instance = copy.deepcopy(step_instance)
        step_instance._configuration = self._step.config
        return step_instance

    def _load_output_materializers(
        self,
    ) -> Dict[str, Tuple[Type[BaseMaterializer], ...]]:
        """Loads the output materializers for the step.

        Returns:
            The step output materializers.
        """
        materializers = {}
        for name, output in self.configuration.outputs.items():
            output_materializers = []

            for source in output.materializer_source:
                materializer_class: Type[
                    BaseMaterializer
                ] = source_utils.load_and_validate_class(
                    source, expected_class=BaseMaterializer
                )
                output_materializers.append(materializer_class)

            materializers[name] = tuple(output_materializers)

        return materializers

    def _parse_inputs(
        self,
        args: List[str],
        annotations: Dict[str, Any],
        input_artifacts: Dict[str, "ArtifactVersionResponse"],
    ) -> Dict[str, Any]:
        """Parses the inputs for a step entrypoint function.

        Args:
            args: The arguments of the step entrypoint function.
            annotations: The annotations of the step entrypoint function.
            input_artifacts: The input artifact versions of the step.

        Returns:
            The parsed inputs for the step entrypoint function.

        Raises:
            RuntimeError: If a function argument value is missing.
        """
        function_params: Dict[str, Any] = {}

        if args and args[0] == "self":
            args.pop(0)

        for arg in args:
            arg_type = annotations.get(arg, None)
            arg_type = resolve_type_annotation(arg_type)

            if inspect.isclass(arg_type) and issubclass(arg_type, StepContext):
                step_name = self.configuration.name
                logger.warning(
                    "Passing a `StepContext` as an argument to a step function "
                    "is deprecated and will be removed in a future release. "
                    f"Please adjust your '{step_name}' step to instead import "
                    "the `StepContext` inside your step, as shown here: "
                    "https://docs.zenml.io/user-guide/advanced-guide/pipelining-features/fetch-metadata-within-steps"
                )
                function_params[arg] = get_step_context()
            elif arg in input_artifacts:
                function_params[arg] = self._load_input_artifact(
                    input_artifacts[arg], arg_type
                )
            elif arg in self.configuration.parameters:
                function_params[arg] = self.configuration.parameters[arg]
            else:
                raise RuntimeError(
                    f"Unable to find value for step function argument `{arg}`."
                )

        return function_params

    def _parse_hook_inputs(
        self,
        args: List[str],
        annotations: Dict[str, Any],
        step_exception: Optional[BaseException],
    ) -> Dict[str, Any]:
        """Parses the inputs for a hook function.

        Args:
            args: The arguments of the hook function.
            annotations: The annotations of the hook function.
            step_exception: The exception of the original step.

        Returns:
            The parsed inputs for the hook function.

        Raises:
            TypeError: If hook function is passed a wrong parameter type.
        """
        from zenml.steps import BaseParameters

        function_params: Dict[str, Any] = {}

        if args and args[0] == "self":
            args.pop(0)

        for arg in args:
            arg_type = annotations.get(arg, None)
            arg_type = resolve_type_annotation(arg_type)

            # Parse the parameters
            if issubclass(arg_type, BaseParameters):
                step_params = arg_type.parse_obj(
                    self.configuration.parameters[arg]
                )
                function_params[arg] = step_params

            # Parse the step context
            elif issubclass(arg_type, StepContext):
                step_name = self.configuration.name
                logger.warning(
                    "Passing a `StepContext` as an argument to a hook function "
                    "is deprecated and will be removed in a future release. "
                    f"Please adjust your '{step_name}' hook to instead import "
                    "the `StepContext` inside your hook, as shown here: "
                    "https://docs.zenml.io/user-guide/advanced-guide/pipelining-features/fetch-metadata-within-steps"
                )
                function_params[arg] = get_step_context()

            elif issubclass(arg_type, BaseException):
                function_params[arg] = step_exception

            else:
                # It should not be of any other type
                raise TypeError(
                    "Hook functions can only take arguments of type "
                    f"`BaseParameters`, or `BaseException`, not {arg_type}"
                )

        return function_params

    def _load_input_artifact(
        self, artifact: "ArtifactVersionResponse", data_type: Type[Any]
    ) -> Any:
        """Loads an input artifact.

        Args:
            artifact: The artifact to load.
            data_type: The data type of the artifact value.

        Returns:
            The artifact value.
        """
        # Skip materialization for `UnmaterializedArtifact`.
        if data_type == UnmaterializedArtifact:
            return UnmaterializedArtifact.parse_obj(artifact)

        if data_type is Any or is_union(get_origin(data_type)):
            # Entrypoint function does not define a specific type for the input,
            # we use the datatype of the stored artifact
            data_type = source_utils.load(artifact.data_type)

        materializer_class: Type[
            BaseMaterializer
        ] = source_utils.load_and_validate_class(
            artifact.materializer, expected_class=BaseMaterializer
        )
        materializer: BaseMaterializer = materializer_class(artifact.uri)
        materializer.validate_type_compatibility(data_type)
        return materializer.load(data_type=data_type)

    def _validate_outputs(
        self,
        return_values: Any,
        output_annotations: Dict[str, OutputSignature],
    ) -> Dict[str, Any]:
        """Validates the step function outputs.

        Args:
            return_values: The return values of the step function.
            output_annotations: The output annotations of the step function.

        Returns:
            The validated output, mapping output names to return values.

        Raises:
            StepInterfaceError: If the step function return values do not
                match the output annotations.
        """
        step_name = self._step.spec.pipeline_parameter_name

        # if there are no outputs, the return value must be `None`.
        if len(output_annotations) == 0:
            if return_values is not None:
                raise StepInterfaceError(
                    f"Wrong step function output type for step '{step_name}': "
                    f"Expected no outputs but the function returned something: "
                    f"{return_values}."
                )
            return {}

        # if there is only one output annotation (either directly specified
        # or contained in an `Output` tuple) we treat the step function
        # return value as the return for that output.
        if len(output_annotations) == 1:
            return_values = [return_values]

        # if the user defined multiple outputs, the return value must be a list
        # or tuple.
        if not isinstance(return_values, (list, tuple)):
            raise StepInterfaceError(
                f"Wrong step function output type for step '{step_name}': "
                f"Expected multiple outputs ({output_annotations}) but "
                f"the function did not return a list or tuple "
                f"(actual return value: {return_values})."
            )

        # The amount of actual outputs must be the same as the amount of
        # expected outputs.
        if len(output_annotations) != len(return_values):
            raise StepInterfaceError(
                f"Wrong amount of step function outputs for step "
                f"'{step_name}: Expected {len(output_annotations)} outputs "
                f"but the function returned {len(return_values)} outputs"
                f"(return values: {return_values})."
            )

        from pydantic.typing import get_origin, is_union

        from zenml.steps.utils import get_args

        validated_outputs: Dict[str, Any] = {}
        for return_value, (output_name, output_annotation) in zip(
            return_values, output_annotations.items()
        ):
            output_type = output_annotation.resolved_annotation
            if output_type is Any:
                pass
            else:
                if is_union(get_origin(output_type)):
                    output_type = get_args(output_type)

                if not isinstance(return_value, output_type):
                    raise StepInterfaceError(
                        f"Wrong type for output '{output_name}' of step "
                        f"'{step_name}' (expected type: {output_type}, "
                        f"actual type: {type(return_value)})."
                    )
            validated_outputs[output_name] = return_value
        return validated_outputs

    def _store_output_artifacts(
        self,
        output_data: Dict[str, Any],
        output_materializers: Dict[str, Tuple[Type[BaseMaterializer], ...]],
        output_artifact_uris: Dict[str, str],
        output_annotations: Dict[str, OutputSignature],
        artifact_metadata_enabled: bool,
        artifact_visualization_enabled: bool,
    ) -> Dict[str, UUID]:
        """Stores the output artifacts of the step.

        Args:
            output_data: The output data of the step function, mapping output
                names to return values.
            output_materializers: The output materializers of the step.
            output_artifact_uris: The output artifact URIs of the step.
            output_annotations: The output annotations of the step function.
            artifact_metadata_enabled: Whether artifact metadata collection is
                enabled.
            artifact_visualization_enabled: Whether artifact visualization is
                enabled.

        Returns:
            The IDs of the published output artifacts.
        """
        step_context = get_step_context()
        output_artifacts: Dict[str, UUID] = {}

        for output_name, return_value in output_data.items():
            data_type = type(return_value)
            materializer_classes = output_materializers[output_name]
            if materializer_classes:
                materializer_class = materializer_utils.select_materializer(
                    data_type=data_type,
                    materializer_classes=materializer_classes,
                )
            else:
                # If no materializer classes are stored in the IR, that means
                # there was no/an `Any` type annotation for the output and
                # we try to find a materializer for it at runtime
                from zenml.materializers.materializer_registry import (
                    materializer_registry,
                )

                default_materializer_source = self._step.config.outputs[
                    output_name
                ].default_materializer_source

                if default_materializer_source:
                    default_materializer_class: Type[
                        BaseMaterializer
                    ] = source_utils.load_and_validate_class(
                        default_materializer_source,
                        expected_class=BaseMaterializer,
                    )
                    materializer_registry.default_materializer = (
                        default_materializer_class
                    )

                materializer_class = materializer_registry[data_type]

            uri = output_artifact_uris[output_name]
            artifact_config = output_annotations[output_name].artifact_config

            if artifact_config is not None:
                has_custom_name = bool(artifact_config.name)
                version = artifact_config.version
            else:
                has_custom_name, version = False, None

            # Override the artifact name if it is not a custom name.
            if has_custom_name:
                artifact_name = output_name
            else:
                if step_context.pipeline_run.pipeline:
                    pipeline_name = step_context.pipeline_run.pipeline.name
                else:
                    pipeline_name = "unlisted"
                step_name = step_context.step_run.name
                artifact_name = f"{pipeline_name}::{step_name}::{output_name}"

            # Get metadata that the user logged manually
            user_metadata = step_context.get_output_metadata(output_name)

            # Get full set of tags
            tags = step_context.get_output_tags(output_name)

            artifact = save_artifact(
                name=artifact_name,
                data=return_value,
                materializer=materializer_class,
                uri=uri,
                extract_metadata=artifact_metadata_enabled,
                include_visualizations=artifact_visualization_enabled,
                has_custom_name=has_custom_name,
                version=version,
                tags=tags,
                user_metadata=user_metadata,
                manual_save=False,
            )
            output_artifacts[output_name] = artifact.id

        return output_artifacts

    def _prepare_model_context_for_step(self) -> None:
        try:
            model = get_step_context().model
            model._get_or_create_model_version()
        except StepContextError:
            return

    def _get_model_versions_from_artifacts(
        self,
        artifact_names: List[str],
    ) -> Set[Tuple[UUID, UUID]]:
        """Gets the model versions from the artifacts.

        Args:
            artifact_names: The names of the published output artifacts.

        Returns:
            Set of tuples of (model_id, model_version_id).
        """
        models = set()
        for artifact_name in artifact_names:
            artifact_config = (
                get_step_context()._get_output(artifact_name).artifact_config
            )
            if artifact_config is not None:
                if (model := artifact_config._model) is not None:
                    model_version_response = (
                        model._get_or_create_model_version()
                    )
                    models.add(
                        (
                            model_version_response.model.id,
                            model_version_response.id,
                        )
                    )
                else:
                    break
        return models

    def _get_model_versions_from_config(self) -> Set[Tuple[UUID, UUID]]:
        """Gets the model versions from the step model version.

        Returns:
            Set of tuples of (model_id, model_version_id).
        """
        try:
            mc = get_step_context().model
            model_version = mc._get_or_create_model_version()
            return {(model_version.model.id, model_version.id)}
        except StepContextError:
            return set()

    def _link_pipeline_run_to_model_from_context(
        self,
        pipeline_run: "PipelineRunResponse",
    ) -> None:
        """Links the pipeline run to the model version using artifacts data.

        Args:
            pipeline_run: The response model of current pipeline run.
        """
        from zenml.models import ModelVersionPipelineRunRequest

        models = self._get_model_versions_from_config()

        client = Client()
        for model in models:
            client.zen_store.create_model_version_pipeline_run_link(
                ModelVersionPipelineRunRequest(
                    user=Client().active_user.id,
                    workspace=Client().active_workspace.id,
                    pipeline_run=pipeline_run.id,
                    model=model[0],
                    model_version=model[1],
                )
            )

    def _link_pipeline_run_to_model_from_artifacts(
        self,
        pipeline_run: "PipelineRunResponse",
        artifact_names: List[str],
        external_artifacts: List["ExternalArtifactConfiguration"],
    ) -> None:
        """Links the pipeline run to the model version using artifacts data.

        Args:
            pipeline_run: The response model of current pipeline run.
            artifact_names: The name of the published output artifacts.
            external_artifacts: The external artifacts of the step.
        """
        from zenml.models import ModelVersionPipelineRunRequest

        models = self._get_model_versions_from_artifacts(artifact_names)
        client = Client()

        # Add models from external artifacts
        for external_artifact in external_artifacts:
            if external_artifact.model:
                models.add(
                    (
                        external_artifact.model.model_id,
                        external_artifact.model.id,
                    )
                )

        for model in models:
            client.zen_store.create_model_version_pipeline_run_link(
                ModelVersionPipelineRunRequest(
                    user=client.active_user.id,
                    workspace=client.active_workspace.id,
                    pipeline_run=pipeline_run.id,
                    model=model[0],
                    model_version=model[1],
                )
            )

    def load_and_run_hook(
        self,
        hook_source: "Source",
        step_exception: Optional[BaseException],
    ) -> None:
        """Loads hook source and runs the hook.

        Args:
            hook_source: The source of the hook function.
            step_exception: The exception of the original step.
        """
        try:
            hook = source_utils.load(hook_source)
            hook_spec = inspect.getfullargspec(inspect.unwrap(hook))

            function_params = self._parse_hook_inputs(
                args=hook_spec.args,
                annotations=hook_spec.annotations,
                step_exception=step_exception,
            )
            logger.debug(f"Running hook {hook} with params: {function_params}")
            hook(**function_params)
        except Exception as e:
            logger.error(
                f"Failed to load hook source with exception: '{hook_source}': "
                f"{e}"
            )
