#  Copyright (c) ZenML GmbH 2021. All Rights Reserved.
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

"""Classes and functions to execute ZenML steps."""

import inspect
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Dict, Sequence, Type

from zenml.artifacts.base_artifact import BaseArtifact
from zenml.client import Client
from zenml.config.pipeline_configurations import PipelineConfiguration
from zenml.config.step_configurations import StepConfiguration
from zenml.config.step_run_info import StepRunInfo
from zenml.enums import ExecutionStatus
from zenml.exceptions import StepInterfaceError
from zenml.logger import get_logger
from zenml.materializers.base_materializer import BaseMaterializer
from zenml.models.artifact_models import ArtifactRequestModel
from zenml.models.step_run_models import StepRunUpdateModel
from zenml.steps.step_context import StepContext
from zenml.steps.step_environment import StepEnvironment
from zenml.steps.utils import (
    parse_return_type_annotations,
    resolve_type_annotation,
)
from zenml.utils import source_utils

if TYPE_CHECKING:
    from uuid import UUID

    from zenml.config.step_configurations import Step
    from zenml.models.step_run_models import StepRunResponseModel

logger = get_logger(__name__)


def register_output_artifacts(
    output_artifacts: Dict[str, BaseArtifact]
) -> Dict[str, "UUID"]:
    """Registers the given output artifacts.

    Args:
        output_artifacts: The output artifacts to register.

    Returns:
        The IDs of the registered output artifacts.
    """
    output_artifact_ids = {}
    for name, artifact_ in output_artifacts.items():
        artifact_model = ArtifactRequestModel(
            name=name,
            type=artifact_.TYPE_NAME,
            uri=artifact_.uri,
            materializer=artifact_.materializer,
            data_type=artifact_.data_type,
        )
        artifact_response = Client().zen_store.create_artifact(artifact_model)
        output_artifact_ids[name] = artifact_response.id
    return output_artifact_ids


class StepExecutor:
    """Class to execute ZenML steps."""

    def __init__(self, step: "Step", step_run: "StepRunResponseModel"):
        """Initializes the step executor.

        Args:
            step: The step to execute.
            step_run: The step run mode.
        """
        self._step = step
        self._step_run = step_run

    @property
    def configuration(self) -> StepConfiguration:
        """Configuration of the step to execute.

        Returns:
            The step configuration.
        """
        return self._step.config

    def _load_step_entrypoint(self) -> Callable[..., Any]:
        """Load the step entrypoint function.

        Returns:
            The step entrypoint function.
        """
        from zenml.steps import BaseStep

        step_class: Type[BaseStep] = source_utils.load_and_validate_class(
            self._step.spec.source, expected_class=BaseStep
        )

        step_instance = step_class()
        step_instance._configuration = self._step.config
        return step_instance.entrypoint

    def _load_output_materializers(self) -> Dict[str, Type[BaseMaterializer]]:
        """Loads the output materializers for the step.

        Returns:
            The step output materializers.
        """
        materializers = {}
        for name, output in self.configuration.outputs.items():
            materializer_class: Type[
                BaseMaterializer
            ] = source_utils.load_and_validate_class(
                output.materializer_source, expected_class=BaseMaterializer
            )
            materializers[name] = materializer_class
        return materializers

    def _load_input_artifact(
        self, artifact: BaseArtifact, data_type: Type[Any]
    ) -> Any:
        """Loads an input artifact.

        Args:
            artifact: The artifact to load.
            data_type: The data type of the artifact value.

        Returns:
            The artifact value.

        Raises:
            RuntimeError: If the artifact has no materializer.
        """
        # Skip materialization for BaseArtifact and its subtypes.
        if issubclass(data_type, BaseArtifact):
            if data_type != type(artifact):
                logger.warning(
                    f"You specified the data_type `{data_type}` but the actual "
                    f"artifact type from the previous step is "
                    f"`{type(artifact)}`. Ignoring this for now, but please be "
                    f"aware of this in your step code."
                )
            return artifact

        if not artifact.materializer:
            raise RuntimeError(
                f"Cannot load input artifact {artifact.name} because it has no "
                "materializer."
            )
        materializer_class = source_utils.load_source_path_class(
            artifact.materializer
        )
        materializer = materializer_class(artifact)
        return materializer.handle_input(data_type=data_type)

    def _store_output_artifact(
        self,
        materializer_class: Type[BaseMaterializer],
        materializer_source: str,
        artifact: BaseArtifact,
        data: Any,
    ) -> None:
        """Stores an output artifact.

        Args:
            materializer_class: The materializer class to store the artifact.
            materializer_source: The source of the materializer class.
            artifact: The artifact to store.
            data: The data to store in the artifact.
        """
        artifact.materializer = materializer_source
        artifact.data_type = source_utils.resolve_class(type(data))
        materializer_class(artifact).handle_return(data)

    def execute(
        self,
        input_artifacts: Dict[str, BaseArtifact],
        output_artifacts: Dict[str, BaseArtifact],
        run_name: str,
        pipeline_config: PipelineConfiguration,
    ) -> None:
        """Executes the step.

        Args:
            input_artifacts: The input artifacts of the step.
            output_artifacts: The output artifacts of the step.
            run_name: The name of the run.
            pipeline_config: The pipeline configuration.

        Raises:
            StepInterfaceError: If the output signature of the step does not
                match the actual output of the step.
        """
        from zenml.steps import BaseParameters

        step_name = self.configuration.name
        step_entrypoint = self._load_step_entrypoint()
        output_materializers = self._load_output_materializers()

        # Building the args for the entrypoint function
        function_params = {}

        # First, we parse the inputs, i.e., params and input artifacts.
        spec = inspect.getfullargspec(inspect.unwrap(step_entrypoint))
        args = spec.args

        if args and args[0] == "self":
            args.pop(0)

        for arg in args:
            arg_type = spec.annotations.get(arg, None)
            arg_type = resolve_type_annotation(arg_type)

            if issubclass(arg_type, BaseParameters):
                step_params = arg_type.parse_obj(self.configuration.parameters)
                function_params[arg] = step_params
            elif issubclass(arg_type, StepContext):
                context = arg_type(
                    step_name=step_name,
                    output_materializers=output_materializers,
                    output_artifacts=output_artifacts,
                )
                function_params[arg] = context
            else:
                # At this point, it has to be an artifact, so we resolve
                function_params[arg] = self._load_input_artifact(
                    input_artifacts[arg], arg_type
                )

        step_run_info = StepRunInfo(
            config=self.configuration,
            pipeline=pipeline_config,
            run_name=run_name,
        )
        # Wrap the execution of the step function in a step environment
        # that the step function code can access to retrieve information about
        # the pipeline runtime, such as the current step name and the current
        # pipeline run ID
        with StepEnvironment(
            pipeline_name=pipeline_config.name,
            pipeline_run_id=run_name,
            step_name=step_name,
            step_run_info=step_run_info,
            cache_enabled=self.configuration.enable_cache,
        ):
            return_values = step_entrypoint(**function_params)

        output_annotations = parse_return_type_annotations(spec.annotations)
        if len(output_annotations) > 0:
            # if there is only one output annotation (either directly specified
            # or contained in an `Output` tuple) we treat the step function
            # return value as the return for that output
            if len(output_annotations) == 1:
                return_values = [return_values]
            elif not isinstance(return_values, Sequence):
                # if the user defined multiple outputs, the return value must
                # be a sequence
                raise StepInterfaceError(
                    f"Wrong step function output type for step '{step_name}: "
                    f"Expected multiple outputs ({output_annotations}) but "
                    f"the function did not return a sequence-like object "
                    f"(actual return value: {return_values})."
                )
            elif len(output_annotations) != len(return_values):
                # if the user defined multiple outputs, the amount of actual
                # outputs must be the same
                raise StepInterfaceError(
                    f"Wrong amount of step function outputs for step "
                    f"'{step_name}: Expected {len(output_annotations)} outputs "
                    f"but the function returned {len(return_values)} outputs"
                    f"(return values: {return_values})."
                )

            for return_value, (output_name, output_type) in zip(
                return_values, output_annotations.items()
            ):
                if not isinstance(return_value, output_type):
                    raise StepInterfaceError(
                        f"Wrong type for output '{output_name}' of step "
                        f"'{step_name}' (expected type: {output_type}, "
                        f"actual type: {type(return_value)})."
                    )

                materializer_class = output_materializers[output_name]
                materializer_source = self.configuration.outputs[
                    output_name
                ].materializer_source

                self._store_output_artifact(
                    materializer_class=materializer_class,
                    materializer_source=materializer_source,
                    artifact=output_artifacts[output_name],
                    data=return_value,
                )

        output_artifact_ids = register_output_artifacts(
            output_artifacts=output_artifacts
        )

        Client().zen_store.update_run_step(
            step_run_id=self._step_run.id,
            step_run_update=StepRunUpdateModel(
                output_artifacts=output_artifact_ids,
                status=ExecutionStatus.COMPLETED,
                end_time=datetime.now(),
            ),
        )