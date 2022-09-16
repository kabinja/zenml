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
"""Model definitions for users, teams, and roles."""

import re
from datetime import datetime, timedelta
from secrets import token_hex
from typing import TYPE_CHECKING, Any, ClassVar, Dict, List, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, SecretStr, root_validator

from zenml.config.global_config import GlobalConfiguration
from zenml.exceptions import AuthorizationException
from zenml.logger import get_logger
from zenml.models.base_models import DomainModel
from zenml.models.constants import MODEL_NAME_FIELD_MAX_LENGTH
from zenml.utils.analytics_utils import AnalyticsTrackedModelMixin
from zenml.utils.enum_utils import StrEnum

logger = get_logger(__name__)

if TYPE_CHECKING:
    from passlib.context import CryptContext  # type: ignore[import]


class JWTTokenType(StrEnum):
    """The type of JWT token."""

    ACCESS_TOKEN = "access_token"


class JWTToken(BaseModel):
    """Pydantic object representing a JWT token.

    Attributes:
        token: The JWT token.
        token_type: The type of token.
    """

    JWT_ALGORITHM: ClassVar[str] = "HS256"

    token_type: JWTTokenType
    user_id: UUID

    @classmethod
    def decode(cls, token_type: JWTTokenType, token: str) -> "JWTToken":
        """Decodes a JWT access token.

        Decodes a JWT access token and returns a `JWTToken` object with the
        information retrieved from its subject claim.

        Args:
            token_type: The type of token.
            token: The encoded JWT token.

        Returns:
            The decoded JWT access token.

        Raises:
            AuthorizationException: If the token is invalid.
        """
        # import here to keep these dependencies out of the client
        from jose import JWTError, jwt  # type: ignore[import]

        try:
            payload = jwt.decode(
                token,
                GlobalConfiguration().jwt_secret_key,
                algorithms=[cls.JWT_ALGORITHM],
            )
        except JWTError as e:
            raise AuthorizationException(f"Invalid JWT token: {e}") from e

        subject: str = payload.get("sub")
        if subject is None:
            raise AuthorizationException(
                "Invalid JWT token: the subject claim is missing"
            )

        try:
            return cls(token_type=token_type, user_id=UUID(subject))
        except ValueError as e:
            raise AuthorizationException(
                f"Invalid JWT token: could not decode subject claim: {e}"
            ) from e

    def encode(self, expire_minutes: Optional[int] = None) -> str:
        """Creates a JWT access token.

        Generates and returns a JWT access token with the subject claim set to
        contain the information in this Pydantic object.

        Args:
            expire_minutes: Number of minutes the token should be valid. If not
                provided, the token will not be set to expire.

        Returns:
            The generated access token.
        """
        # import here to keep these dependencies out of the client
        from jose import jwt

        claims = {
            "sub": str(self.user_id),
        }

        if expire_minutes:
            expire = datetime.utcnow() + timedelta(minutes=expire_minutes)
            claims["exp"] = expire

        token = jwt.encode(
            claims,
            GlobalConfiguration().jwt_secret_key,
            algorithm=self.JWT_ALGORITHM,
        )
        return token


class UserModel(DomainModel, AnalyticsTrackedModelMixin):
    """Domain model for user accounts."""

    ANALYTICS_FIELDS: ClassVar[List[str]] = [
        "id",
        "name",
        "full_name",
        "email",
        "active",
    ]

    name: str = Field(
        default="",
        title="The unique username for the account.",
        max_length=MODEL_NAME_FIELD_MAX_LENGTH,
    )
    full_name: str = Field(
        default="",
        title="The full name for the account owner.",
        max_length=MODEL_NAME_FIELD_MAX_LENGTH,
    )
    email: str = Field(
        default="",
        title="The email address associated with the account.",
        max_length=MODEL_NAME_FIELD_MAX_LENGTH,
    )
    active: bool = Field(default=False, title="Active account.")
    password: Optional[SecretStr] = Field(default=None, exclude=True)
    activation_token: Optional[SecretStr] = Field(default=None, exclude=True)

    @classmethod
    def _get_crypt_context(cls) -> "CryptContext":
        """Returns the password encryption context.

        Returns:
            The password encryption context.
        """
        from passlib.context import CryptContext

        return CryptContext(schemes=["bcrypt"], deprecated="auto")

    @classmethod
    def verify_password(
        cls, plain_password: str, user: Optional["UserModel"] = None
    ) -> bool:
        """Verifies a given plain password against the stored password.

        Args:
            plain_password: Input password to be verified.
            user: User for which the password is to be verified.

        Returns:
            True if the passwords match.
        """
        # even when the user or password is not set, we still want to execute
        # the password hash verification to protect against response discrepancy
        # attacks (https://cwe.mitre.org/data/definitions/204.html)
        hash: Optional[str] = None
        if user is not None and user.password is not None and user.active:
            hash = user.get_hashed_password()
        pwd_context = cls._get_crypt_context()
        return pwd_context.verify(plain_password, hash)

    def get_password(self) -> Optional[str]:
        """Get the password.

        Returns:
            The password as a plain string, if it exists.
        """
        if self.password is None:
            return None
        return self.password.get_secret_value()

    @classmethod
    def _is_hashed_secret(cls, secret: SecretStr) -> bool:
        """Checks if a secret value is already hashed.

        Args:
            secret: The secret value to check.

        Returns:
            True if the secret value is hashed, otherwise False.
        """
        return (
            re.match(r"^\$2[ayb]\$.{56}$", secret.get_secret_value())
            is not None
        )

    @classmethod
    def _get_hashed_secret(cls, secret: Optional[SecretStr]) -> Optional[str]:
        """Hashes the input secret and returns the hash value, if supplied and if not already hashed.

        Args:
            secret: The secret value to hash.

        Returns:
            The secret hash value, or None if no secret was supplied.
        """
        if secret is None:
            return None
        if cls._is_hashed_secret(secret):
            return secret.get_secret_value()
        pwd_context = cls._get_crypt_context()
        return pwd_context.hash(secret.get_secret_value())

    def get_hashed_password(self) -> Optional[str]:
        """Returns the hashed password, if configured.

        Returns:
            The hashed password.
        """
        return self._get_hashed_secret(self.password)

    @classmethod
    def verify_access_token(cls, token: str) -> Optional["UserModel"]:
        """Verifies an access token.

        Verifies an access token and returns the user that was used to generate
        it if the token is valid and None otherwise.

        Args:
            token: The access token to verify.

        Returns:
            The user that generated the token if valid, None otherwise.
        """
        try:
            access_token = JWTToken.decode(
                token_type=JWTTokenType.ACCESS_TOKEN, token=token
            )
        except AuthorizationException:
            return None

        zen_store = GlobalConfiguration().zen_store
        try:
            user = zen_store.get_user(user_name_or_id=access_token.user_id)
        except KeyError:
            return None

        if access_token.user_id == user.id and user.active:
            return user

        return None

    def generate_access_token(self) -> str:
        """Generates an access token.

        Generates an access token and returns it.

        Returns:
            The generated access token.
        """
        return JWTToken(
            token_type=JWTTokenType.ACCESS_TOKEN, user_id=self.id
        ).encode()

    def get_activation_token(self) -> Optional[str]:
        """Get the activation token.

        Returns:
            The activation token as a plain string, if it exists.
        """
        if self.activation_token is None:
            return None
        return self.activation_token.get_secret_value()

    def get_hashed_activation_token(self) -> str:
        """Returns the hashed activation token, if configured.

        Returns:
            The hashed activation token.
        """
        return self._get_hashed_secret(self.activation_token)

    @classmethod
    def verify_activation_token(
        cls, activation_token: str, user: Optional["UserModel"] = None
    ) -> bool:
        """Verifies a given activation token against the stored activation token.

        Args:
            activation_token: Input activation token to be verified.
            user: User for which the activation token is to be verified.

        Returns:
            True if the token is valid.
        """
        # even when the user or token is not set, we still want to execute the
        # token hash verification to protect against response discrepancy
        # attacks (https://cwe.mitre.org/data/definitions/204.html)
        hash: Optional[str] = None
        if (
            user is not None
            and user.activation_token is not None
            and not user.active
        ):
            hash = user.get_hashed_activation_token()
        pwd_context = cls._get_crypt_context()
        return pwd_context.verify(activation_token, hash)

    def generate_activation_token(self) -> str:
        """Generates and stores a new activation token.

        Returns:
            The generated activation token.
        """
        self.activation_token = token_hex(32)
        return self.activation_token

    class Config:
        """Pydantic configuration class."""

        # Validate attributes when assigning them
        validate_assignment = True
        # Forbid extra attributes to prevent unexpected behavior
        extra = "forbid"


class RoleModel(DomainModel, AnalyticsTrackedModelMixin):
    """Domain model for roles."""

    ANALYTICS_FIELDS: ClassVar[List[str]] = ["id"]

    name: str = Field(
        title="The unique name of the role.",
        max_length=MODEL_NAME_FIELD_MAX_LENGTH,
    )


class TeamModel(DomainModel, AnalyticsTrackedModelMixin):
    """Domain model for teams."""

    ANALYTICS_FIELDS: ClassVar[List[str]] = ["id"]

    name: str = Field(
        title="The unique name of the team.",
        max_length=MODEL_NAME_FIELD_MAX_LENGTH,
    )


class RoleAssignmentModel(BaseModel):
    """Pydantic object representing a role assignment.

    Attributes:
        id: Id of the role assignment.
        role_id: Id of the role.
        project_id: Optional ID of a project that the role is limited to.
        team_id: Id of a team to which the role is assigned.
        user_id: Id of a user to which the role is assigned.
        created_at: Date when the role was assigned.
    """

    id: Optional[UUID] = None
    role_id: UUID
    project_id: Optional[UUID] = None
    team_id: Optional[UUID] = None
    user_id: Optional[UUID] = None
    created_at: Optional[datetime] = None

    @root_validator
    def ensure_single_entity(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        """Validates that either `user_id` or `team_id` is set.

        Args:
            values: The values to validate.

        Returns:
            The validated values.

        Raises:
            ValueError: If neither `user_id` nor `team_id` is set.
        """
        user_id = values.get("user_id", None)
        team_id = values.get("team_id", None)
        if user_id and team_id:
            raise ValueError("Only `user_id` or `team_id` is allowed.")

        if not (user_id or team_id):
            raise ValueError(
                "Missing `user_id` or `team_id` for role assignment."
            )

        return values
