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
import logging
import uuid
from copy import deepcopy
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Type, TypeVar

from pydantic import BaseModel, Field, SecretStr

from tests.integration.functional.utils import sample_name
from zenml.client import Client
from zenml.config.global_config import GlobalConfiguration
from zenml.config.pipeline_configurations import PipelineConfiguration
from zenml.config.pipeline_spec import PipelineSpec
from zenml.config.store_config import StoreConfiguration
from zenml.enums import (
    ArtifactType,
    SecretScope,
    StackComponentType,
)
from zenml.exceptions import IllegalOperationError
from zenml.models import (
    APIKeyRequest,
    ArtifactFilter,
    ArtifactRequest,
    ArtifactUpdate,
    ArtifactVersionFilter,
    ArtifactVersionRequest,
    ArtifactVersionUpdate,
    AuthenticationMethodModel,
    BaseFilter,
    BaseRequest,
    BaseResponse,
    CodeRepositoryFilter,
    CodeRepositoryRequest,
    CodeRepositoryUpdate,
    ComponentFilter,
    ComponentRequest,
    ComponentUpdate,
    FlavorFilter,
    FlavorRequest,
    ModelFilter,
    ModelRequest,
    ModelUpdate,
    ModelVersionRequest,
    Page,
    PipelineBuildFilter,
    PipelineBuildRequest,
    PipelineDeploymentFilter,
    PipelineDeploymentRequest,
    PipelineFilter,
    PipelineRequest,
    PipelineRunFilter,
    PipelineRunRequest,
    ResourceTypeModel,
    SecretFilter,
    SecretRequest,
    ServiceAccountRequest,
    ServiceConnectorFilter,
    ServiceConnectorRequest,
    ServiceConnectorTypeModel,
    ServiceConnectorUpdate,
    StackRequest,
    StepRunFilter,
    UserFilter,
    UserRequest,
    UserUpdate,
    WorkspaceFilter,
    WorkspaceRequest,
    WorkspaceUpdate,
)
from zenml.pipelines import pipeline
from zenml.service_connectors.service_connector import AuthenticationConfig
from zenml.service_connectors.service_connector_registry import (
    service_connector_registry,
)
from zenml.steps import step
from zenml.utils.string_utils import random_str


@step
def constant_int_output_test_step() -> int:
    logging.info("log")
    return 7


@step
def int_plus_one_test_step(input: int) -> int:
    return input + 1


@pipeline(name="connected_two_step_pipeline")
def connected_two_step_pipeline(step_1, step_2):
    """Pytest fixture that returns a pipeline which takes two steps
    `step_1` and `step_2` that are connected."""
    step_2(step_1())


pipeline_instance = connected_two_step_pipeline(
    step_1=constant_int_output_test_step(),
    step_2=int_plus_one_test_step(),
)


class PipelineRunContext:
    """Context manager that creates pipeline runs and cleans them up afterwards."""

    def __init__(self, num_runs: int, enable_step_logs: bool = True):
        self.num_runs = num_runs
        self.client = Client()
        self.store = self.client.zen_store
        self.enable_step_logs = enable_step_logs

    def __enter__(self):
        self.pipeline_name = sample_name("sample_pipeline_run_")
        for i in range(self.num_runs):
            pipeline_instance.run(
                run_name=f"{self.pipeline_name}_{i}",
                unlisted=True,
                enable_step_logs=self.enable_step_logs,
            )

        # persist which runs, steps and artifact versions were produced.
        # In case the test ends up deleting some or all of these, this allows
        # for a thorough cleanup nonetheless
        self.runs = self.store.list_runs(
            PipelineRunFilter(name=f"startswith:{self.pipeline_name}")
        ).items
        self.steps = []
        self.artifact_versions = []
        for run in self.runs:
            self.steps += self.store.list_run_steps(
                StepRunFilter(pipeline_run_id=run.id)
            ).items
            for s in self.steps:
                self.artifact_versions += [a for a in s.outputs.values()]
        return self.runs

    def __exit__(self, exc_type, exc_value, exc_traceback):
        for run in self.runs:
            try:
                self.client.delete_pipeline_run(run.id)
            except KeyError:
                pass
        for artifact_version in self.artifact_versions:
            try:
                self.client.delete_artifact_version(artifact_version.id)
            except KeyError:
                pass
            try:
                artifact = self.client.get_artifact(
                    artifact_version.artifact.id
                )
                if not artifact.versions:
                    self.client.delete_artifact(artifact.id)
            except KeyError:
                pass


class UserContext:
    def __init__(
        self,
        user_name: Optional[str] = None,
        password: Optional[str] = None,
        inactive: bool = False,
        login: bool = False,
        existing_user: bool = False,
        delete: bool = True,
    ):
        if existing_user:
            self.user_name = user_name
        elif user_name:
            self.user_name = user_name
        else:
            self.user_name = sample_name("aria")
        self.client = Client()
        self.store = self.client.zen_store
        self.login = login
        if inactive and password is None:
            self.password = None
        else:
            self.password = password or random_str(32)
        self.existing_user = existing_user
        self.delete = delete

    def __enter__(self):
        if not self.existing_user:
            new_user = UserRequest(
                name=self.user_name, password=self.password, active=True
            )
            self.created_user = self.store.create_user(new_user)
        else:
            self.created_user = self.store.get_user(self.user_name)

        if self.login or self.existing_user:
            self.original_config = GlobalConfiguration.get_instance()
            self.original_client = Client.get_instance()

            GlobalConfiguration._reset_instance()
            Client._reset_instance()
            self.client = Client()
            store_config = StoreConfiguration(
                url=self.original_config.store.url,
                type=self.original_config.store.type,
                username=self.user_name,
                password=self.password,
                secrets_store=self.original_config.store.secrets_store,
            )
            GlobalConfiguration().set_store(config=store_config)
        return self.created_user

    def __exit__(self, exc_type, exc_value, exc_traceback):
        if self.login or self.existing_user:
            GlobalConfiguration._reset_instance(self.original_config)
            Client._reset_instance(self.original_client)
            _ = Client().zen_store
        if not self.existing_user and self.delete:
            try:
                self.store.delete_user(self.created_user.id)
            except (KeyError, IllegalOperationError):
                pass


class ServiceAccountContext:
    def __init__(
        self,
        name: str = "aria",
        description: str = "Aria's service account",
        login: bool = False,
        existing_account: bool = False,
        delete: bool = True,
    ):
        if existing_account:
            self.name = name
        else:
            self.name = sample_name(name)
        self.description = description
        self.client = Client()
        self.store = self.client.zen_store
        self.login = login
        self.existing_account = existing_account
        self.delete = delete

    def __enter__(self):
        if not self.existing_account:
            new_account = ServiceAccountRequest(
                name=self.name,
                description=self.description,
                active=True,
            )
            self.created_service_account = self.store.create_service_account(
                new_account
            )
        else:
            self.created_service_account = self.store.get_service_account(
                self.name
            )

        if self.login or self.existing_account:
            # Create a temporary API key for the service account
            api_key_name = sample_name("temp_api_key")
            self.api_key = self.store.create_api_key(
                self.created_service_account.id,
                APIKeyRequest(
                    name=api_key_name,
                ),
            )
            self.original_config = GlobalConfiguration.get_instance()
            self.original_client = Client.get_instance()

            GlobalConfiguration._reset_instance()
            Client._reset_instance()
            self.client = Client()
            store_config = StoreConfiguration(
                url=self.original_config.store.url,
                type=self.original_config.store.type,
                api_key=self.api_key.key,
                secrets_store=self.original_config.store.secrets_store,
            )
            GlobalConfiguration().set_store(config=store_config)
        return self.created_service_account

    def __exit__(self, exc_type, exc_value, exc_traceback):
        if self.login or self.existing_account:
            GlobalConfiguration._reset_instance(self.original_config)
            Client._reset_instance(self.original_client)
            _ = Client().zen_store
        if self.existing_account or self.login and self.delete:
            self.store.delete_api_key(
                self.created_service_account.id,
                self.api_key.id,
            )
        if not self.existing_account and self.delete:
            try:
                self.store.delete_service_account(
                    self.created_service_account.id
                )
            except (KeyError, IllegalOperationError):
                pass


class LoginContext:
    def __init__(
        self,
        user_name: Optional[str] = None,
        password: Optional[str] = None,
        api_key: Optional[str] = None,
    ):
        self.user_name = user_name
        self.password = password
        self.api_key = api_key

    def __enter__(self):
        self.original_config = GlobalConfiguration.get_instance()
        self.original_client = Client.get_instance()

        GlobalConfiguration._reset_instance()
        Client._reset_instance()
        store_config = StoreConfiguration(
            url=self.original_config.store.url,
            type=self.original_config.store.type,
            api_key=self.api_key,
            username=self.user_name,
            password=self.password,
            secrets_store=self.original_config.store.secrets_store,
        )
        GlobalConfiguration().set_store(config=store_config)

    def __exit__(self, exc_type, exc_value, exc_traceback):
        GlobalConfiguration._reset_instance(self.original_config)
        Client._reset_instance(self.original_client)
        _ = Client().zen_store


class StackContext:
    def __init__(
        self,
        components: Dict[StackComponentType, List[uuid.UUID]],
        stack_name: str = "aria",
        user_id: Optional[uuid.UUID] = None,
        delete: bool = True,
    ):
        self.stack_name = sample_name(stack_name)
        self.user_id = user_id
        self.components = components
        self.client = Client()
        self.store = self.client.zen_store
        self.delete = delete

    def __enter__(self):
        new_stack = StackRequest(
            user=self.user_id if self.user_id else self.client.active_user.id,
            workspace=self.client.active_workspace.id,
            name=self.stack_name,
            components=self.components,
        )
        self.created_stack = self.store.create_stack(new_stack)
        return self.created_stack

    def cleanup(self):
        try:
            self.store.delete_stack(self.created_stack.id)
        except KeyError:
            pass

    def __exit__(self, exc_type, exc_value, exc_traceback):
        if self.delete:
            self.cleanup()


class ComponentContext:
    def __init__(
        self,
        c_type: StackComponentType,
        config: Dict[str, Any],
        flavor: str,
        component_name: str = "aria",
        user_id: Optional[uuid.UUID] = None,
        delete: bool = True,
    ):
        self.component_name = sample_name(component_name)
        self.flavor = flavor
        self.component_type = c_type
        self.config = config
        self.user_id = user_id
        self.client = Client()
        self.store = self.client.zen_store
        self.delete = delete

    def __enter__(self):
        new_component = ComponentRequest(
            user=self.user_id if self.user_id else self.client.active_user.id,
            workspace=self.client.active_workspace.id,
            name=self.component_name,
            type=self.component_type,
            flavor=self.flavor,
            configuration=self.config,
        )
        self.created_component = self.store.create_stack_component(
            new_component
        )
        return self.created_component

    def cleanup(self):
        try:
            self.store.delete_stack_component(self.created_component.id)
        except KeyError:
            pass

    def __exit__(self, exc_type, exc_value, exc_traceback):
        if self.delete:
            self.cleanup()


class WorkspaceContext:
    def __init__(
        self,
        workspace_name: str = "super_axl",
        create: bool = True,
        activate: bool = False,
    ):
        self.workspace_name = (
            sample_name(workspace_name) if create else workspace_name
        )
        self.client = Client()
        self.store = self.client.zen_store
        self.create = create
        self.activate = activate

    def __enter__(self):
        if self.create:
            new_workspace = WorkspaceRequest(name=self.workspace_name)
            self.workspace = self.store.create_workspace(new_workspace)
        else:
            self.workspace = self.store.get_workspace(self.workspace_name)

        if self.activate:
            self.original_workspace = self.client.active_workspace
            self.client.set_active_workspace(self.workspace.id)
        return self.workspace

    def __exit__(self, exc_type, exc_value, exc_traceback):
        if self.activate:
            self.client.set_active_workspace(self.original_workspace.id)
        if self.create:
            try:
                self.store.delete_workspace(self.workspace.id)
            except KeyError:
                pass


class SecretContext:
    def __init__(
        self,
        secret_name: Optional[str] = None,
        scope: SecretScope = SecretScope.WORKSPACE,
        values: Dict[str, str] = {
            "sleep": "yes",
            "food": "hell yeah",
            "bath": "NO!",
        },
        user_id: Optional[uuid.UUID] = None,
        workspace_id: Optional[uuid.UUID] = None,
        delete: bool = True,
    ):
        self.secret_name = (
            sample_name("axls-secrets") if not secret_name else secret_name
        )
        self.scope = scope
        self.values = values
        self.user_id = user_id
        self.workspace_id = workspace_id
        self.client = Client()
        self.store = self.client.zen_store
        self.delete = delete

    def __enter__(self):
        new_secret = SecretRequest(
            name=self.secret_name,
            scope=self.scope,
            values=self.values,
            user=self.user_id or self.client.active_user.id,
            workspace=self.workspace_id or self.client.active_workspace.id,
        )
        self.created_secret = self.store.create_secret(new_secret)
        return self.created_secret

    def cleanup(self):
        try:
            self.store.delete_secret(self.created_secret.id)
        except KeyError:
            pass

    def __exit__(self, exc_type, exc_value, exc_traceback):
        if self.delete:
            self.cleanup()


class CodeRepositoryContext:
    def __init__(
        self,
        user_id: Optional[uuid.UUID] = None,
        workspace_id: Optional[uuid.UUID] = None,
        delete: bool = True,
    ):
        self.code_repo_name = sample_name("code_repo")
        self.user_id = user_id
        self.workspace_id = workspace_id
        self.client = Client()
        self.store = self.client.zen_store
        self.delete = delete

    def __enter__(self):
        request = CodeRepositoryRequest(
            name=self.code_repo_name,
            config={},
            source={
                "module": "tests.unit.pipelines.test_build_utils",
                "attribute": "StubCodeRepository",
                "type": "user",
            },
            user=self.user_id or self.client.active_user.id,
            workspace=self.workspace_id or self.client.active_workspace.id,
        )

        self.repo = self.store.create_code_repository(request)
        return self.repo

    def cleanup(self):
        try:
            self.store.delete_code_repository(self.repo.id)
        except KeyError:
            pass

    def __exit__(self, exc_type, exc_value, exc_traceback):
        if self.delete:
            self.cleanup()


class ServiceConnectorContext:
    def __init__(
        self,
        connector_type: str,
        auth_method: str,
        resource_types: List[str],
        name: Optional[str] = None,
        resource_id: Optional[str] = None,
        configuration: Optional[Dict[str, str]] = None,
        secrets: Optional[Dict[str, Optional[SecretStr]]] = None,
        expires_at: Optional[datetime] = None,
        expires_skew_tolerance: Optional[int] = None,
        expiration_seconds: Optional[int] = None,
        user_id: Optional[uuid.UUID] = None,
        workspace_id: Optional[uuid.UUID] = None,
        labels: Optional[Dict[str, str]] = None,
        client: Optional[Client] = None,
        delete: bool = True,
    ):
        self.name = name or sample_name("connect-or")
        self.connector_type = connector_type
        self.auth_method = auth_method
        self.resource_types = resource_types
        self.resource_id = resource_id
        self.configuration = configuration
        self.secrets = secrets
        self.expires_at = expires_at
        self.expires_skew_tolerance = expires_skew_tolerance
        self.expiration_seconds = expiration_seconds
        self.user_id = user_id
        self.workspace_id = workspace_id
        self.labels = labels
        self.client = client or Client()
        self.store = self.client.zen_store
        self.delete = delete

    def __enter__(self):
        request = ServiceConnectorRequest(
            name=self.name,
            connector_type=self.connector_type,
            auth_method=self.auth_method,
            resource_types=self.resource_types,
            resource_id=self.resource_id,
            configuration=self.configuration or {},
            secrets=self.secrets or {},
            expires_at=self.expires_at,
            expires_skew_tolerance=self.expires_skew_tolerance,
            expiration_seconds=self.expiration_seconds,
            labels=self.labels or {},
            user=self.user_id or self.client.active_user.id,
            workspace=self.workspace_id or self.client.active_workspace.id,
        )

        self.connector = self.store.create_service_connector(request)
        return self.connector

    def cleanup(self):
        try:
            self.store.delete_service_connector(self.connector.id)
        except KeyError:
            pass

    def __exit__(self, exc_type, exc_value, exc_traceback):
        if self.delete:
            self.cleanup()


class ModelContext:
    def __init__(
        self,
        create_version: bool = False,
        create_artifacts: int = 0,
        create_prs: int = 0,
        user_id: Optional[uuid.UUID] = None,
        delete: bool = True,
    ):
        client = Client()
        self.workspace = client.active_workspace.id
        self.user = user_id or client.active_user.id
        self.model = sample_name("su_model")
        self.model_version = "2.0.0"

        self.create_version = create_version
        self.create_artifacts = create_artifacts
        self.artifacts = []
        self.artifact_versions = []
        self.create_prs = create_prs
        self.prs = []
        self.deployments = []
        self.delete = delete

    def __enter__(self):
        client = Client()
        ws = client.get_workspace(self.workspace)
        user = client.get_user(self.user)
        stack = client.active_stack
        try:
            model = client.get_model(self.model)
        except KeyError:
            model = client.create_model(name=self.model, tags=["foo", "bar"])
        if self.create_version:
            try:
                mv = client.get_model_version(self.model, self.model_version)
            except KeyError:
                mv = client.zen_store.create_model_version(
                    ModelVersionRequest(
                        user=user.id,
                        workspace=ws.id,
                        model=model.id,
                        name=self.model_version,
                    )
                )

        for _ in range(self.create_artifacts):
            artifact = client.zen_store.create_artifact(
                ArtifactRequest(
                    name=sample_name("sample_artifact"),
                    has_custom_name=True,
                )
            )
            client.get_artifact(artifact.id)
            self.artifacts.append(artifact)
            artifact_version = client.zen_store.create_artifact_version(
                ArtifactVersionRequest(
                    artifact_id=artifact.id,
                    version=1,
                    data_type="module.class",
                    materializer="module.class",
                    type=ArtifactType.DATA,
                    uri="",
                    user=user.id,
                    workspace=ws.id,
                )
            )
            self.artifact_versions.append(artifact_version)
        for _ in range(self.create_prs):
            deployment = client.zen_store.create_deployment(
                PipelineDeploymentRequest(
                    user=user.id,
                    workspace=ws.id,
                    stack=stack.id,
                    run_name_template="",
                    pipeline_configuration={"name": "pipeline_name"},
                    client_version="0.12.3",
                    server_version="0.12.3",
                ),
            )
            self.deployments.append(deployment)
            self.prs.append(
                client.zen_store.create_run(
                    PipelineRunRequest(
                        id=uuid.uuid4(),
                        name=sample_name("sample_pipeline_run"),
                        status="running",
                        config=PipelineConfiguration(name="aria_pipeline"),
                        user=user.id,
                        workspace=ws.id,
                        deployment=deployment.id,
                    )
                )
            )
        if self.create_version:
            if self.create_artifacts:
                return mv, self.artifact_versions
            if self.create_prs:
                return mv, self.prs
            else:
                return mv
        else:
            if self.create_artifacts:
                return model, self.artifact_versions
            if self.create_prs:
                return model, self.prs
            else:
                return model

    def cleanup(self):
        client = Client()
        try:
            client.delete_model(self.model)
        except KeyError:
            pass
        for artifact_version in self.artifact_versions:
            client.delete_artifact_version(artifact_version.id)
        for artifact in self.artifacts:
            client.delete_artifact(artifact.id)
        for run in self.prs:
            client.zen_store.delete_run(run.id)
        for deployment in self.deployments:
            client.delete_deployment(str(deployment.id))

    def __exit__(self, exc_type, exc_value, exc_traceback):
        if self.delete:
            self.cleanup()


class CatClawMarks(AuthenticationConfig):
    """Cat claw marks authentication credentials."""

    paw: SecretStr = Field(
        title="Paw",
    )
    hiding_spot: Optional[SecretStr] = Field(
        default=None,
        title="Hiding spot",
    )
    color: Optional[str] = Field(
        default=None,
        title="Cat color.",
    )
    name: str = Field(
        title="Cat name.",
    )


class CatVoicePrint(AuthenticationConfig):
    """Cat voice-print authentication credentials."""

    secret_word: SecretStr = Field(
        title="Secret word",
    )
    hiding_spot: Optional[SecretStr] = Field(
        default=None,
        title="Hiding spot",
    )
    color: Optional[str] = Field(
        default=None,
        title="Cat color.",
    )
    name: str = Field(
        title="Cat name.",
    )


class ServiceConnectorTypeContext:
    def __init__(
        self,
        connector_type: Optional[str] = None,
        resource_type_one: Optional[str] = None,
        resource_type_two: Optional[str] = None,
        delete: bool = True,
    ):
        self.connector_type = connector_type
        self.resource_type_one = resource_type_one
        self.resource_type_two = resource_type_two
        self.delete = delete

    def __enter__(self):
        self.connector_type_spec = ServiceConnectorTypeModel(
            name="Cat service connector",
            connector_type=self.connector_type or sample_name("cat'o'matic"),
            auth_methods=[
                AuthenticationMethodModel(
                    name="Claw marks authentication",
                    auth_method="claw-marks",
                    config_class=CatClawMarks,
                ),
                AuthenticationMethodModel(
                    name="Voice print authentication",
                    auth_method="voice-print",
                    config_class=CatVoicePrint,
                ),
            ],
            resource_types=[
                ResourceTypeModel(
                    name="Cat scratches",
                    resource_type=self.resource_type_one
                    or sample_name("scratch"),
                    auth_methods=["claw-marks", "voice-print"],
                    supports_instances=True,
                ),
                ResourceTypeModel(
                    name="Cat purrs",
                    resource_type=self.resource_type_two
                    or sample_name("purr"),
                    auth_methods=["claw-marks", "voice-print"],
                    supports_instances=False,
                ),
            ],
        )

        service_connector_registry.register_service_connector_type(
            self.connector_type_spec
        )

        return self.connector_type_spec

    def __exit__(self, exc_type, exc_value, exc_traceback):
        if self.delete:
            try:
                del service_connector_registry.service_connector_types[
                    self.connector_type
                ]
            except KeyError:
                pass


AnyRequest = TypeVar("AnyRequest", bound=BaseRequest)
AnyResponse = TypeVar("AnyResponse", bound=BaseResponse)


class CrudTestConfig:
    """Model to collect all methods pertaining to a given entity."""

    def __init__(
        self,
        create_model: "BaseModel",
        filter_model: Type[BaseFilter],
        entity_name: str,
        update_model: Optional["BaseModel"] = None,
        conditional_entities: Optional[Dict[str, "CrudTestConfig"]] = None,
    ):
        """Initializes a CrudTestConfig.

        Args:
            create_model: Model to use for creating the entity.
            update_model: Model to use for updating the entity.
            filter_model: Model to use for filtering entities.
            entity_name: Name of the entity.
            conditional_entity_names: Names of entities that need to exist
                before the entity under test can be created.
            conditional_entities: Other entities that need to exist before the
                entity under test can be created. Expected to be a mapping from
                field in the `create_model` to corresponding `CrudTestConfig`.
        """
        self.create_model = create_model
        self.update_model = update_model
        self.filter_model = filter_model
        self.entity_name = entity_name
        self.conditional_entities = conditional_entities or {}
        self.id: Optional[uuid.UUID] = None

    @property
    def list_method(
        self,
    ) -> Callable[[BaseFilter], Page[AnyResponse]]:
        store = Client().zen_store
        if self.entity_name.endswith("y"):
            method_name = f"list_{self.entity_name[:-1]}ies"
        else:
            method_name = f"list_{self.entity_name}s"
        return getattr(store, method_name)

    @property
    def get_method(self) -> Callable[[uuid.UUID], AnyResponse]:
        store = Client().zen_store
        return getattr(store, f"get_{self.entity_name}")

    @property
    def delete_method(self) -> Callable[[uuid.UUID], None]:
        store = Client().zen_store
        return getattr(store, f"delete_{self.entity_name}")

    @property
    def create_method(self) -> Callable[[AnyRequest], AnyResponse]:
        store = Client().zen_store
        return getattr(store, f"create_{self.entity_name}")

    @property
    def update_method(
        self,
    ) -> Callable[[uuid.UUID, BaseModel], AnyResponse]:
        store = Client().zen_store
        return getattr(store, f"update_{self.entity_name}")

    def create(self) -> AnyResponse:
        """Creates the entity."""
        create_model = self.create_model

        # Set active user, workspace, and stack if applicable
        client = Client()
        if hasattr(create_model, "user"):
            create_model.user = client.active_user.id
        if hasattr(create_model, "workspace"):
            create_model.workspace = client.active_workspace.id
        if hasattr(create_model, "stack"):
            create_model.stack = client.active_stack_model.id

        # create other required entities if applicable
        for (
            field_name,
            conditional_entity,
        ) in self.conditional_entities.items():
            setattr(create_model, field_name, conditional_entity.create().id)

        # Create the entity itself
        response = self.create_method(create_model)
        self.id = response.id
        return response

    def list(self) -> Page[AnyResponse]:
        """Lists all entities."""
        return self.list_method(self.filter_model())

    def get(self) -> AnyResponse:
        """Gets the entity if it was already created."""
        if not self.id:
            raise ValueError("Entity not created yet.")
        return self.get_method(self.id)

    def update(self) -> AnyResponse:
        """Updates the entity if it was already created."""
        if not self.id:
            raise ValueError("Entity not created yet.")
        if not self.update_model:
            raise NotImplementedError("This entity cannot be updated.")
        return self.update_method(self.id, self.update_model)

    def delete(self) -> None:
        """Deletes the entity if it was already created."""
        if not self.id:
            raise ValueError("Entity not created yet.")
        self.delete_method(self.id)
        self.id = None

    def cleanup(self) -> None:
        """Deletes all entities that were created, including itself."""
        if self.id:
            self.delete()
        for conditional_entity in self.conditional_entities.values():
            conditional_entity.cleanup()


workspace_crud_test_config = CrudTestConfig(
    create_model=WorkspaceRequest(name=sample_name("sample_workspace")),
    update_model=WorkspaceUpdate(name=sample_name("updated_sample_workspace")),
    filter_model=WorkspaceFilter,
    entity_name="workspace",
)
user_crud_test_config = CrudTestConfig(
    create_model=UserRequest(name=sample_name("sample_user")),
    update_model=UserUpdate(name=sample_name("updated_sample_user")),
    filter_model=UserFilter,
    entity_name="user",
)
flavor_crud_test_config = CrudTestConfig(
    create_model=FlavorRequest(
        name=sample_name("sample_flavor"),
        type=StackComponentType.ORCHESTRATOR,
        integration="",
        source="",
        config_schema="",
        user=uuid.uuid4(),
        workspace=uuid.uuid4(),
    ),
    filter_model=FlavorFilter,
    entity_name="flavor",
)
component_crud_test_config = CrudTestConfig(
    create_model=ComponentRequest(
        name=sample_name("sample_component"),
        type=StackComponentType.ORCHESTRATOR,
        flavor="local",
        configuration={},
        user=uuid.uuid4(),
        workspace=uuid.uuid4(),
    ),
    update_model=ComponentUpdate(name=sample_name("updated_sample_component")),
    filter_model=ComponentFilter,
    entity_name="stack_component",
)
pipeline_crud_test_config = CrudTestConfig(
    create_model=PipelineRequest(
        name=sample_name("sample_pipeline"),
        spec=PipelineSpec(steps=[]),
        user=uuid.uuid4(),
        workspace=uuid.uuid4(),
        version="1",
        version_hash="abc123",
    ),
    # Updating pipelines is not doing anything at the moment
    # update_model=PipelineUpdate(name=sample_name("updated_sample_pipeline")),
    filter_model=PipelineFilter,
    entity_name="pipeline",
)
# pipeline_run_crud_test_config = CrudTestConfig(
#     create_model=PipelineRunRequestModel(
#         id=uuid.uuid4(),
#         deployment=uuid.uuid4(), # deployment has to exist first
#         pipeline=uuid.uuid4(),
#         name=sample_name("sample_pipeline_run"),
#         status=ExecutionStatus.RUNNING,
#         config=PipelineConfiguration(name="aria_pipeline"),
#         user=uuid.uuid4(),
#         workspace=uuid.uuid4(),
#     ),
#     update_model=PipelineRunUpdateModel(status=ExecutionStatus.COMPLETED),
#     filter_model=PipelineRunFilterModel,
#     entity_name="run",
# )
artifact_crud_test_config = CrudTestConfig(
    entity_name="artifact",
    create_model=ArtifactRequest(
        name=sample_name("sample_artifact"),
        has_custom_name=True,
    ),
    filter_model=ArtifactFilter,
    update_model=ArtifactUpdate(
        name=sample_name("sample_artifact"),
        add_tags=["tag1", "tag2"],
    ),
)
artifact_version_crud_test_config = CrudTestConfig(
    entity_name="artifact_version",
    create_model=ArtifactVersionRequest(
        artifact_id=uuid.uuid4(),  # will be overridden in create()
        version=1,
        data_type="module.class",
        materializer="module.class",
        type=ArtifactType.DATA,
        uri="",
        user=uuid.uuid4(),
        workspace=uuid.uuid4(),
    ),
    filter_model=ArtifactVersionFilter,
    update_model=ArtifactVersionUpdate(add_tags=["tag1", "tag2"]),
    conditional_entities={"artifact_id": deepcopy(artifact_crud_test_config)},
)
secret_crud_test_config = CrudTestConfig(
    create_model=SecretRequest(
        name=sample_name("sample_secret"),
        values={"key": "value"},
        user=uuid.uuid4(),
        workspace=uuid.uuid4(),
    ),
    filter_model=SecretFilter,
    entity_name="secret",
)
build_crud_test_config = CrudTestConfig(
    create_model=PipelineBuildRequest(
        user=uuid.uuid4(),
        workspace=uuid.uuid4(),
        images={},
        is_local=False,
        contains_code=True,
    ),
    filter_model=PipelineBuildFilter,
    entity_name="build",
)
deployment_crud_test_config = CrudTestConfig(
    create_model=PipelineDeploymentRequest(
        user=uuid.uuid4(),
        workspace=uuid.uuid4(),
        stack=uuid.uuid4(),
        run_name_template="template",
        pipeline_configuration={"name": "pipeline_name"},
        client_version="0.12.3",
        server_version="0.12.3",
    ),
    filter_model=PipelineDeploymentFilter,
    entity_name="deployment",
)
code_repository_crud_test_config = CrudTestConfig(
    create_model=CodeRepositoryRequest(
        user=uuid.uuid4(),
        workspace=uuid.uuid4(),
        name=sample_name("sample_code_repository"),
        config={},
        source={"module": "module", "type": "user"},
    ),
    update_model=CodeRepositoryUpdate(
        name=sample_name("updated_sample_code_repository")
    ),
    filter_model=CodeRepositoryFilter,
    entity_name="code_repository",
)
service_connector_crud_test_config = CrudTestConfig(
    create_model=ServiceConnectorRequest(
        user=uuid.uuid4(),
        workspace=uuid.uuid4(),
        name=sample_name("sample_service_connector"),
        connector_type="docker",
        auth_method="password",
        configuration=dict(
            username="user",
            password="password",
        ),
    ),
    update_model=ServiceConnectorUpdate(
        name=sample_name("updated_sample_service_connector"),
    ),
    filter_model=ServiceConnectorFilter,
    entity_name="service_connector",
)
model_crud_test_config = CrudTestConfig(
    create_model=ModelRequest(
        user=uuid.uuid4(),
        workspace=uuid.uuid4(),
        name="super_model",
        license="who cares",
        description="cool stuff",
        audience="world",
        use_cases="all",
        limitations="none",
        trade_offs="secret",
        ethics="all good",
        tags=["cool", "stuff"],
        save_models_to_registry=True,
    ),
    update_model=ModelUpdate(
        name=sample_name("updated_sample_service_connector"),
        description="new_description",
    ),
    filter_model=ModelFilter,
    entity_name="model",
)

# step_run_crud_test_config = CrudTestConfig(
#     create_model=StepRunRequestModel(
#         name=sample_name("sample_step_run"),
#         step=Step(
#             spec=StepSpec(source="", upstream_steps=[], inputs=[]),
#             config=StepConfiguration(name="sample_step_run")
#         ),
#         status=ExecutionStatus.RUNNING,
#         user=uuid.uuid4(),
#         workspace=uuid.uuid4(),
#         pipeline_run_id=uuid.uuid4()   # Pipeline run with id needs to exist
#     ),
#     update_model=StepRunUpdateModel(status=ExecutionStatus.COMPLETED),
#     filter_model=StepRunFilterModel,
#     entity_name="run_step",
# )


list_of_entities = [
    workspace_crud_test_config,
    user_crud_test_config,
    flavor_crud_test_config,
    component_crud_test_config,
    pipeline_crud_test_config,
    # step_run_crud_test_config,
    # pipeline_run_crud_test_config,
    artifact_crud_test_config,
    artifact_version_crud_test_config,
    secret_crud_test_config,
    build_crud_test_config,
    deployment_crud_test_config,
    code_repository_crud_test_config,
    service_connector_crud_test_config,
    model_crud_test_config,
]
