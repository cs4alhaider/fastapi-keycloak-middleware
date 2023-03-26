"""
This module contains the Keycloak backend.

It is used by the middleware to perform the actual authentication.
"""
import logging
import typing
from typing import Tuple

import keycloak
from keycloak import KeycloakOpenID
from starlette.authentication import AuthCredentials, AuthenticationBackend, BaseUser
from starlette.requests import HTTPConnection

from fastapi_keycloak_middleware.exceptions import (
    AuthClaimMissing,
    AuthHeaderMissing,
    AuthInvalidToken,
    AuthKeycloakError,
    AuthUserError,
)
from fastapi_keycloak_middleware.fast_api_user import FastApiUser
from fastapi_keycloak_middleware.schemas.authorization_methods import (
    AuthorizationMethod,
)
from fastapi_keycloak_middleware.schemas.keycloak_configuration import (
    KeycloakConfiguration,
)

log = logging.getLogger(__name__)


class KeycloakBackend(AuthenticationBackend):  # pylint: disable=too-few-public-methods
    """
    Backend to perform authentication using Keycloak
    """

    def __init__(
        self,
        keycloak_configuration: KeycloakConfiguration,
        user_mapper: typing.Callable[[typing.Dict[str, typing.Any]], typing.Awaitable[typing.Any]],
        use_introspection_endpoint: bool = True,
    ):
        self.keycloak_configuration = keycloak_configuration
        self.use_introspection_endpoint = use_introspection_endpoint
        self.get_user = user_mapper if user_mapper else self._get_user

    async def _get_user(self, userinfo: typing.Dict[str, typing.Any]) -> BaseUser:
        """
        Default implementation of the get_user method.
        """
        return FastApiUser(
            first_name=userinfo.get("given_name", ""),
            last_name=userinfo.get("family_name", ""),
            user_id=userinfo.get("user_id", ""),
        )

    async def authenticate(self, conn: HTTPConnection) -> Tuple[AuthCredentials, BaseUser]:
        """
        The authenticate method is invoked each time a route is called that
        the middleware is applied to.
        """
        if "Authorization" not in conn.headers:
            raise AuthHeaderMissing

        # Check if token starts with the authentication scheme
        auth_header = conn.headers["Authorization"]
        token = auth_header.split(" ")
        if len(token) != 2 or token[0] != self.keycloak_configuration.authentication_scheme:
            raise AuthInvalidToken

        token_info = None

        # Depending on the chosen method by the user, either
        # use the introspection endpoint or decode the token
        if self.use_introspection_endpoint:
            log.debug("Using introspection endpoint to validate token")
            # Call introspect endpoint to check if token is valid
            keycloak_openid = KeycloakOpenID(
                server_url=self.keycloak_configuration.url,
                client_id=self.keycloak_configuration.client_id,
                realm_name=self.keycloak_configuration.realm,
                client_secret_key=self.keycloak_configuration.client_secret,
            )
            try:
                token_info = keycloak_openid.introspect(token[1])
            except keycloak.exceptions.KeycloakPostError as exc:
                raise AuthKeycloakError from exc
        else:
            log.debug("Using keycloak public key to validate token")
            # Decode Token locally using the public key
            kc_public_key = (
                "-----BEGIN PUBLIC KEY-----\n"
                + keycloak_openid.public_key()
                + "\n-----END PUBLIC KEY-----"
            )
            options = {"verify_signature": True, "verify_aud": True, "verify_exp": True}
            token_info = keycloak_openid.decode_token(token[1], key=kc_public_key, options=options)

        # Extract claims from token
        user_info = {}
        for claim in self.keycloak_configuration.claims:
            try:
                user_info[claim] = token_info[claim]
            except KeyError:
                log.warning("Claim %s is configured but missing in the token", claim)
                if self.keycloak_configuration.reject_on_missing_claim:
                    log.warning("Rejecting request because of missing claim")
                    raise AuthClaimMissing from KeyError
                log.debug("Backend is configured to ignore missing claims, continuing...")

        # Call user function to get user object
        try:
            user = await self.get_user(user_info)
        except Exception as exc:
            log.warning(
                "Error while getting user object: %s. "
                "The user-provided function raised an exception",
                exc,
            )
            raise AuthUserError from exc

        if not user:
            log.warning("User object is None. The user-provided function returned None")
            raise AuthUserError

        # Handle Authorization depending on the Claim Method
        scope_auth = []
        if self.keycloak_configuration.authorization_method == AuthorizationMethod.CLAIM:
            if self.keycloak_configuration.authorization_claim not in token_info:
                raise AuthClaimMissing
            scope_auth = token_info[self.keycloak_configuration.authorization_claim]

        return scope_auth, user