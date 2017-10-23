import logging
from requests import Session
from requests.auth import HTTPBasicAuth
from ..common.security import generate_token, is_secure_transport
from ..common.urls import url_decode
from ..specs.rfc6749.client import (
    prepare_grant_uri,
    prepare_token_request,
    parse_authorization_code_response,
)
from ..specs.rfc6749 import OAuth2Token
from ..specs.rfc6749 import OAuth2Error, InsecureTransportError
from ..specs.rfc6750 import BearToken

__all__ = ['OAuth2Session']

log = logging.getLogger(__name__)
DEFAULT_HEADERS = {
    'Accept': 'application/json',
    'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8'
}


class TokenError(OAuth2Error):
    def __init__(self, error=None, description=None, status_code=None,
                 uri=None, state=None, **kwargs):

        if error is not None:
            self.error = error
        super(TokenError).__init__(description, status_code, uri,
                                   state, **kwargs)


class OAuth2Session(Session):
    def __init__(self, client_id=None, client_secret=None,
                 auto_refresh_url=None, auto_refresh_kwargs=None,
                 scope=None, redirect_uri=None,
                 token=None, token_placement='headers',
                 state=None, token_updater=None):
        """Construct a new OAuth 2 client requests session.

        :param client_id:
        :param client_secret:
        :param auto_refresh_url:
        :param auto_refresh_kwargs:
        :param scope:
        :param redirect_uri: Redirect URI you registered as callback.
        :param token: A dict of token attributes such as ``access_token``,
                      ``token_type`` and ``expires_at``.
        :param state: State string used to prevent CSRF. This will be given
                      when creating the authorization url and must be
                      supplied when parsing the authorization response.
        :param token_updater:
        """
        super(OAuth2Session, self).__init__()

        self.client_id = client_id
        self.client_secret = client_secret
        self.auto_refresh_url = auto_refresh_url
        self.auto_refresh_kwargs = auto_refresh_kwargs
        self.scope = scope
        self.redirect_uri = redirect_uri
        self.token = token
        self.token_placement = token_placement
        self.state = state
        self.token_updater = token_updater

        self.compliance_hook = {
            'access_token_response': set(),
            'refresh_token_response': set(),
            'protected_request': set(),
        }

    @property
    def token_cls(self):
        if not self.token:
            return None

        token_type = self.token['token_type'].lower()
        if token_type == 'bearer':
            return BearToken

    def authorization_url(self, url, state=None, **kwargs):
        if state is None:
            state = generate_token()

        uri = prepare_grant_uri(
            url, redirect_uri=self.redirect_uri,
            scope=self.scope, state=state, **kwargs
        )
        return uri, state

    def fetch_access_token(
            self, url=None, code=None, authorization_response=None,
            body='', auth=None, username=None, password=None, method='POST',
            timeout=None, headers=None, verify=True, proxies=None, **kwargs):

        if url is None and authorization_response:
            return self.token_from_fragment(authorization_response)

        if not is_secure_transport(url):
            raise InsecureTransportError()

        if not code and authorization_response:
            params = parse_authorization_code_response(
                authorization_response,
                state=self.state
            )
            code = params['code']

        body = prepare_token_request(
            'authorization_code',
            code=code, body=body,
            client_id=self.client_id,
            redirect_uri=self.redirect_uri,
            **kwargs
        )

        client_id = kwargs.get('client_id', '')
        if auth is None:
            if client_id:
                client_secret = kwargs.get('client_secret', '')
                if client_secret is None:
                    client_secret = ''
                auth = HTTPBasicAuth(client_id, client_secret)
            elif username:
                if password is None:
                    raise ValueError('Username was supplied, but not password.')
                auth = HTTPBasicAuth(username, password)

        if headers is None:
            headers = DEFAULT_HEADERS

        if method.upper() == 'POST':
            resp = self.post(
                url, data=dict(url_decode(body)), timeout=timeout,
                headers=headers, auth=auth, verify=verify, proxies=proxies
            )
        else:
            resp = self.get(
                url, params=dict(url_decode(body)), timeout=timeout,
                headers=headers, auth=auth, verify=verify, proxies=proxies
            )

        for hook in self.compliance_hook['access_token_response']:
            resp = hook(resp)

        params = resp.json()
        if 'error' not in params:
            return OAuth2Token(params)

        error = params['error']
        description = params.get('description')
        uri = params.get('error_uri'),
        state = params.get('state')
        raise TokenError(error, description, resp.status_code, uri, state)

    def fetch_token(self, url, **kwargs):
        """Alias for fetch_access_token. Compatible with requests-oauthlib."""
        return self.fetch_access_token(url, **kwargs)

    def token_from_fragment(self, authorization_response):
        pass

    def refresh_token(self, url, **kwargs):
        pass

    def request(self, method, url, data=None, headers=None,
                withhold_token=False, **kwargs):

        if self.token and not withhold_token:
            tok = self.token_cls(self.token['access_token'])
            url, headers, data = tok.add_token(
                url, headers, data, self.token_placement
            )

            for hook in self.compliance_hook['protected_request']:
                url, headers, data = hook(url, headers, data)

            # TODO: auto renew
        return super(OAuth2Session, self).request(
            method, url, headers=headers, data=data, **kwargs)

    def register_compliance_hook(self, hook_type, hook):
        """Register a hook for request/response tweaking.

        Available hooks are:
        * access_token_response: invoked before token parsing.
        * refresh_token_response: invoked before refresh token parsing.
        * protected_request: invoked before making a request.
        """
        if hook_type not in self.compliance_hook:
            raise ValueError('Hook type %s is not in %s.',
                             hook_type, self.compliance_hook)
        self.compliance_hook[hook_type].add(hook)