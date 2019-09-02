# This file provides common configuration for the different ways that
# the deployment can run. Configuration specific to the different modes
# will be read from separate files at the end of this configuration
# file.

import os
import json

import requests
import wrapt

from kubernetes.client.rest import ApiException

from kubernetes.client.configuration import Configuration
from kubernetes.config.incluster_config import load_incluster_config
from kubernetes.client.api_client import ApiClient
from openshift.dynamic import DynamicClient

# Helper function for doing unit conversions or translations if needed.

def convert_size_to_bytes(size):
    multipliers = {
        'k': 1000,
        'm': 1000**2,
        'g': 1000**3,
        't': 1000**4,
        'ki': 1024,
        'mi': 1024**2,
        'gi': 1024**3,
        'ti': 1024**4,
    }

    size = str(size)

    for suffix in multipliers:
        if size.lower().endswith(suffix):
            return int(size[0:-len(suffix)]) * multipliers[suffix]
    else:
        if size.lower().endswith('b'):
            return int(size[0:-1])

    try:
        return int(size)
    except ValueError:
        raise RuntimeError('"%s" is not a valid memory specification. Must be an integer or a string with suffix K, M, G, T, Ki, Mi, Gi or Ti.' % size)

# Work out the name of the namespace in which we are being deployed.
# deployment is in.

service_account_path = '/var/run/secrets/kubernetes.io/serviceaccount'

with open(os.path.join(service_account_path, 'namespace')) as fp:
    namespace = fp.read().strip()

# Initialise client for the REST API used doing configuration.
#
# XXX Currently have a workaround here for OpenShift 4.0 beta versions
# which disables verification of the certificate. If don't use this the
# Python openshift/kubernetes clients will fail. We also disable any
# warnings from urllib3 to get rid of the noise in the logs this creates.

load_incluster_config()

import urllib3
urllib3.disable_warnings()
instance = Configuration()
instance.verify_ssl = False
Configuration.set_default(instance)

api_client = DynamicClient(ApiClient())

image_stream_resource = api_client.resources.get(
     api_version='image.openshift.io/v1', kind='ImageStream')

# Helper function for determining the correct name for the image. We
# need to do this for references to image streams because of the image
# lookup policy often not being correctly setup on OpenShift clusters.

def resolve_image_name(name):
    # If the image name contains a slash, we assume it is already
    # referring to an image on some image registry. Even if it does
    # not contain a slash, it may still be hosted on docker.io.

    if name.find('/') != -1:
        return name

    # Separate actual source image name and tag for the image from the
    # name. If the tag is not supplied, default to 'latest'.

    parts = name.split(':', 1)

    if len(parts) == 1:
        source_image, tag = parts, 'latest'
    else:
        source_image, tag = parts

    # See if there is an image stream in the current project with the
    # target name.

    try:
        image_stream = image_stream_resource.get(namespace=namespace,
                name=source_image)

    except ApiException as e:
        if e.status not in (403, 404):
            raise

        return name

    # If we get here then the image stream exists with the target name.
    # We need to determine if the tag exists. If it does exist, we
    # extract out the full name of the image including the reference
    # to the image registry it is hosted on.

    if image_stream.status.tags:
        for entry in image_stream.status.tags:
            if entry.tag == tag:
                registry_image = image_stream.status.dockerImageRepository
                if registry_image:
                    return '%s:%s' % (registry_image, tag)

    # Use original value if can't find a matching tag.

    return name

# The application name and configuration type are passed in through the
# template. The application name should be the value used for the
# deployment, and more specifically, must match the name of the route.
# The configuration type will vary based on the template, as the setup
# required for each will be different.

application_name = os.environ.get('APPLICATION_NAME', 'homeroom')

configuration_type = os.environ.get('CONFIGURATION_TYPE', 'hosted-workshop')

# Define all the defaults for for JupyterHub instance for our setup.

c.JupyterHub.port = 8080

c.JupyterHub.hub_ip = '0.0.0.0'
c.JupyterHub.hub_port = 8081

c.JupyterHub.hub_connect_ip = application_name

c.ConfigurableHTTPProxy.api_url = 'http://127.0.0.1:8082'

c.Spawner.start_timeout = 120
c.Spawner.http_timeout = 60

c.KubeSpawner.port = 8080

c.KubeSpawner.common_labels = { 'app': application_name }

c.KubeSpawner.uid = os.getuid()
c.KubeSpawner.fs_gid = os.getuid()

c.KubeSpawner.extra_annotations = {
    "alpha.image.policy.openshift.io/resolve-names": "*"
}

c.KubeSpawner.cmd = ['start-singleuser.sh']

c.KubeSpawner.pod_name_template = '%s-nb-{username}' % application_name

c.JupyterHub.admin_access = True

if os.environ.get('JUPYTERHUB_COOKIE_SECRET'):
    c.JupyterHub.cookie_secret = os.environ[
            'JUPYTERHUB_COOKIE_SECRET'].encode('UTF-8')
else:
    c.JupyterHub.cookie_secret_file = '/opt/app-root/data/cookie_secret'

c.JupyterHub.db_url = '/opt/app-root/data/database.sqlite'

c.JupyterHub.authenticator_class = 'tmpauthenticator.TmpAuthenticator'

c.JupyterHub.spawner_class = 'kubespawner.KubeSpawner'

c.KubeSpawner.image_spec = resolve_image_name(
        os.environ.get('JUPYTERHUB_NOTEBOOK_IMAGE',
        's2i-minimal-notebook:3.6'))

if os.environ.get('JUPYTERHUB_NOTEBOOK_MEMORY'):
    c.Spawner.mem_limit = convert_size_to_bytes(os.environ['JUPYTERHUB_NOTEBOOK_MEMORY'])

# Workaround bug in minishift where a service cannot be contacted from a
# pod which backs the service. For further details see the minishift issue
# https://github.com/minishift/minishift/issues/2400.
#
# What these workarounds do is monkey patch the JupyterHub proxy client
# API code, and the code for creating the environment for local service
# processes, and when it sees something which uses the service name as
# the target in a URL, it replaces it with localhost. These work because
# the proxy/service processes are in the same pod. It is not possible to
# change hub_connect_ip to localhost because that is passed to other
# pods which need to contact back to JupyterHub, and so it must be left
# as the service name.

@wrapt.patch_function_wrapper('jupyterhub.proxy', 'ConfigurableHTTPProxy.add_route')
def _wrapper_add_route(wrapped, instance, args, kwargs):
    def _extract_args(routespec, target, data, *_args, **_kwargs):
        return (routespec, target, data, _args, _kwargs)

    routespec, target, data, _args, _kwargs = _extract_args(*args, **kwargs)

    old = 'http://%s:%s' % (c.JupyterHub.hub_connect_ip, c.JupyterHub.hub_port)
    new = 'http://127.0.0.1:%s' % c.JupyterHub.hub_port

    if target.startswith(old):
        target = target.replace(old, new)

    return wrapped(routespec, target, data, *_args, **_kwargs)

@wrapt.patch_function_wrapper('jupyterhub.spawner', 'LocalProcessSpawner.get_env')
def _wrapper_get_env(wrapped, instance, args, kwargs):
    env = wrapped(*args, **kwargs)

    target = env.get('JUPYTERHUB_API_URL')

    old = 'http://%s:%s' % (c.JupyterHub.hub_connect_ip, c.JupyterHub.hub_port)
    new = 'http://127.0.0.1:%s' % c.JupyterHub.hub_port

    if target and target.startswith(old):
        target = target.replace(old, new)
        env['JUPYTERHUB_API_URL'] = target

    return env

# Kubernetes REST API endpoint and cluster information.

kubernetes_service_host = os.environ['KUBERNETES_SERVICE_HOST']
kubernetes_service_port = os.environ['KUBERNETES_SERVICE_PORT']

kubernetes_server_url = 'https://%s:%s' % (kubernetes_service_host,
        kubernetes_service_port)

kubernetes_server_version_url = '%s/version' % kubernetes_server_url

with requests.Session() as session:
    response = session.get(kubernetes_server_version_url, verify=False)
    kubernetes_server_info = json.loads(response.content.decode('UTF-8'))

image_registry = 'image-registry.openshift-image-registry.svc:5000'

if kubernetes_server_info['major'] == '1':
    if kubernetes_server_info['minor'] in ('10', '10+', '11', '11+'):
        image_registry = 'docker-registry.default.svc:5000'

# Override styling elements for the JupyterHub web pages.

c.JupyterHub.logo_file = '/opt/app-root/src/images/HomeroomIcon.png'

# Work out the service account name and name of the namespace that the
# deployment is in.

service_account_name = '%s-hub' % application_name

service_account_path = '/var/run/secrets/kubernetes.io/serviceaccount'

with open(os.path.join(service_account_path, 'namespace')) as fp:
    namespace = fp.read().strip()

# Override the default name template for the pod so we include both the
# application name and namespace. Don't change this as 'event' module
# relies on it being this name to make it easier to match up a pod to
# the temporary project created for that session.

c.KubeSpawner.pod_name_template = '%s-%s-{username}' % (
        application_name, namespace)

# Use a higher port number for the terminals spawned by JupyterHub so
# that secondary applications run from within the terminal can use port
# 8080 for testing, or so that port can be exposed by a separate route.

c.KubeSpawner.port = 10080

# Initialise the set of environment variables to be empty so we know it
# is a dict. This is so we can incrementally add values as we go along.

c.Spawner.environment = dict()

# Initialise the set of services to be empty so we know it is a list.
# This is so we can incrementally add values as we go along.

c.JupyterHub.services = []

# Initialise the set of init containers to be empty so we know it is
# a list. This is so we can incrementally add values as we go along.

c.KubeSpawner.init_containers = []

# Initialise the set of extra containers to be empty so we know it is
# a list. This is so we can incrementally add values as we go along.

c.KubeSpawner.extra_containers = []

# Initialise the set of extra handlers to be empty so we know it is
# a list. This is so we can incrementally add values as we go along.

c.JupyterHub.extra_handlers = []

# Override the image details with that for the terminal or dashboard
# image being used. The default is to assume that a image stream with
# the same name as the application name is being used. The call to the
# function resolve_image_name() is to try and resolve to image registry
# when using image stream. This is to workaround issue that many
# clusters do not have image policy controller configured correctly.
#
# Note that we set the policy that images will always be pulled to the
# node each time when the image name is not explicitly provided. This is
# so that during development, changes to the terminal image will always
# be picked up. Someone developing a new image need only update the
# 'latest' tag on the image using 'oc tag'. 

terminal_image = os.environ.get('TERMINAL_IMAGE')

if not terminal_image:
    c.KubeSpawner.image_pull_policy = 'Always'
    terminal_image = '%s:latest' % application_name

c.KubeSpawner.image_spec = resolve_image_name(terminal_image)

# Set the default amount of memory provided to a pod. This might be
# overridden on a case by case for images if a profile list is supplied
# so users have a choice of images when deploying workshop content.

c.Spawner.mem_limit = convert_size_to_bytes(
        os.environ.get('WORKSHOP_MEMORY', '512Mi'))

# Work out hostname for the exposed route of the JupyterHub server. This
# is tricky as we need to use the REST API to query it. We assume that
# a secure route is always used. This is used when needing to do OAuth.

route_resource = api_client.resources.get(
     api_version='route.openshift.io/v1', kind='Route')

routes = route_resource.get(namespace=namespace)

def extract_hostname(routes, name):
    for route in routes.items:
        if route.metadata.name == name:
            return route.spec.host

public_hostname = extract_hostname(routes, application_name)

if not public_hostname:
    raise RuntimeError('Cannot calculate external host name for JupyterHub.')

c.Spawner.environment['JUPYTERHUB_ROUTE'] = 'https://%s' % public_hostname

# Work out the subdomain under which applications hosted in the cluster
# are hosted. Calculate this from the route for the JupyterHub route if
# not supplied explicitly.

cluster_subdomain = os.environ.get('CLUSTER_SUBDOMAIN')

if not cluster_subdomain:
    cluster_subdomain = '.'.join(public_hostname.split('.')[1:])

c.Spawner.environment['CLUSTER_SUBDOMAIN'] = cluster_subdomain

# The terminal image will normally work out what versions of OpenShift
# and Kubernetes command line tools should be used, based on the version
# of OpenShift which is being used. Allow these to be overridden if
# necessary.

if os.environ.get('OC_VERSION'):
    c.Spawner.environment['OC_VERSION'] = os.environ.get('OC_VERSION')
if os.environ.get('ODO_VERSION'):
    c.Spawner.environment['ODO_VERSION'] = os.environ.get('ODO_VERSION')
if os.environ.get('KUBECTL_VERSION'):
    c.Spawner.environment['KUBECTL_VERSION'] = os.environ.get('KUBECTL_VERSION')

# Load configuration corresponding to the deployment mode.

c.Spawner.environment['DEPLOYMENT_TYPE'] = 'spawner'
c.Spawner.environment['CONFIGURATION_TYPE'] = configuration_type

config_root = '/opt/app-root/src/configs'
config_file = '%s/%s.py' % (config_root, configuration_type)

if os.path.exists(config_file):
    with open(config_file) as fp:
        exec(compile(fp.read(), config_file, 'exec'), globals())

# Load configuration provided via the environment.

environ_config_file = '/opt/app-root/configs/jupyterhub_config.py'

if os.path.exists(environ_config_file):
    with open(environ_config_file) as fp:
        exec(compile(fp.read(), environ_config_file, 'exec'), globals())
