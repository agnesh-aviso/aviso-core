"""
URL configuration for aviso_core project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
import os
import logging

from django.contrib import admin
from django.urls import path, include, re_path

from gbm_apis.api.auth_tokens import (GetAuthorizationTokenDetails,
                                      SetAuthenticationToken)


logger = logging.getLogger(f'gnana.{__name__}')

urlpatterns = [
    path('gbm/', include('gbm_apis.urls')),
    path('sdk/', include('sdk.urls')),
    path('', include('accounts.urls')),
    # Token registration for tenant onboarding. At the root, not under /gbm/:
    # the onboarding tool calls shell.api('/set_auth_token', ...) and
    # shell.api('/get_auth_token', None), the paths the legacy service used.
    path('set_auth_token', SetAuthenticationToken.as_view(), name='set_auth_token'),
    path('get_auth_token', GetAuthorizationTokenDetails.as_view(), name='get_auth_token'),
]
