# Single sign-on

Northwind Cloud supports single sign-on with **SAML 2.0** and **OpenID Connect**.
SSO is available on Team and Enterprise plans.

Automatic user provisioning and deprovisioning with **SCIM 2.0** requires the
**Enterprise** plan. Team plans can use SSO login but users must be invited
manually. Just-in-time provisioning is not supported.

SSO sessions last 12 hours. Administrators can force all sessions to sign out
from Settings → Security.
