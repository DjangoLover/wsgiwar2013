import hashlib
import os
import random
import shutil
import urllib

from browserid.errors import TrustError
import colander
from pyramid.httpexceptions import HTTPBadRequest
from pyramid.httpexceptions import HTTPFound
from pyramid.response import FileResponse
from pyramid.session import check_csrf_token
from pyramid.view import view_config
from pyramid.security import (
    forget,
    remember,
)
from pyramid.traversal import resource_path
from substanced.db import root_factory
from substanced.interfaces import IUserLocator
from substanced.principal import DefaultUserLocator
from substanced.util import find_service
from substanced.util import get_oid

from ..utils import get_redis


random.seed()


@view_config(content_type='Song',
             name="record",
             renderer="templates/record.pt")
def recording_app(song, request):
    recording_id = generate_recording_id({})
    return {
        "id": recording_id,
        "mp3_url": request.resource_url(song, 'mp3'),
        "timings": song.timings,
    }


@view_config(content_type='Song', name="record", xhr=True, renderer='string')
def save_recording(song, request):
    f = request.params['data'].file
    id = request.params['id']
    fname = request.params['filename']
    tmpdir = '/tmp/' + id
    if not os.path.exists(tmpdir):
        os.mkdir(tmpdir)
    with open('%s/%s' % (tmpdir, fname), 'wb') as output:
        shutil.copyfileobj(f, output)
    return 'OK'


@view_config(content_type='Song', name="record", xhr=True, renderer='string',
             request_param='finished')
def finish_recording(song, request):
    tmpdir = '/tmp/' + request.params['id']
    recording = request.registry.content.create('Recording', tmpdir)
    recordings = request.root['recordings']
    name = generate_recording_id(recordings)
    recordings[name] = recording
    recording.performer = request.user
    recording.song = song

    redis = get_redis(request)
    print "finished", tmpdir, resource_path(recording)
    redis.rpush("yss.new-recordings", resource_path(recording))
    return request.resource_url(recording)


idchars = (
    map(chr, range(ord('a'), ord('z') + 1)) +
    map(chr, range(ord('A'), ord('Z') + 1)) +
    map(chr, range(ord('0'), ord('9') + 1)))


def generate_recording_id(recordings):
    while True:
        id = ''.join([random.choice(idchars) for _ in range(8)])
        if id not in recordings:
            break
    return id


@view_config(
    context='velruse.AuthenticationComplete',
)
def velruse_login_complete_view(context, request):
    provider = context.provider_name
    profile = context.profile
    username = profile['accounts'][0]['username']
    root = root_factory(request)
    adapter = request.registry.queryMultiAdapter(
        (root, request), IUserLocator)
    if adapter is None:
        adapter = DefaultUserLocator(root, request)
    user = adapter.get_user_by_login(username)
    if user is None:
        registry = request.registry
        principals = find_service(root, 'principals')
        user = principals.add_user(username, registry=registry)
        performer = registry.content.create('Performer')
        performer.display_name = performer.title = profile['displayName']
        addresses = profile.get('addresses')
        if addresses:
            user.email = performer.email = addresses[0]['formatted']
        photos = profile.get('photos')
        if photos:
            performer.photo_url = photos[0]['value']
        performer.age = colander.null
        performer.sex = user.favorite_genre = None
        root['performers'][username] = performer
        performer.user = user
        location = request.resource_url(performer, 'edit.html')
    else:
        location = request.resource_url(root['performers'][username])
    headers = remember(request, get_oid(user))
    return HTTPFound(location, headers=headers)

@view_config(name='logout')
def velruse_logout(request):
    headers = forget(request)
    try:
        del request.user
    except AttributeError:
        pass
    return HTTPFound(location=request.resource_url(request.virtual_root),
                     headers=headers,
                    )


@view_config(
    context='velruse.AuthenticationDenied',
    renderer='templates/login_denied.pt',
)
def velruse_login_denied_view(context, request):
    return {
        'ok': False,
        'provider_name': context.provider_name,
        'reason': context.reason,
    }

# Persona integration

_PERSONA_SIGNIN_HTML = (
    '<img src="https://login.persona.org/i/persona_sign_in_blue.png" '
          'id="persona-signin" alt="sign-in button" />')

_PERSONA_SIGNOUT_HTML = '<button id="persona-signout">logout</button>'

PERSONA_JS = """
$(function() {
    $('#persona-signin').click(function() {
        navigator.id.request(%(request_params)s);
        return false;
    });

    $('#persona-signout').click(function() {
        navigator.id.logout();
        return false;
    });

    var currentUser = %(user)s;

    navigator.id.watch({
        loggedInUser: currentUser,
        onlogin: function(assertion) {
            $.ajax({
                type: 'POST',
                url: '%(login)s',
                data: {
                    assertion: assertion,
                    came_from: '%(came_from)s',
                    csrf_token: '%(csrf_token)s'
                },
                dataType: 'json',
                success: function(res, status, xhr) {
                    if(!res['success'])
                        navigator.id.logout();
                    window.location = res['redirect'];
                },
                error: function(xhr, status, err) {
                    navigator.id.logout();
                    alert("Login failure: " + err);
                }
            });
        },
        onlogout: function() {
            $.ajax({
                type: 'POST',
                url: '%(logout)s',
                data:{
                    came_from: '%(came_from)s',
                    csrf_token: '%(csrf_token)s'
                },
                dataType: 'json',
                success: function(res, status, xhr) {
                    window.location = res['redirect'];
                },
                error: function(xhr, status, err) {
                    alert("Logout failure: " + err);
                }
            });
        }
    });
});
"""


def persona_js(request):
    """Return the javascript needed to run persona.
    """
    user = request.user
    if user is None:
        userid = 'null'
    else:
        userid = "'%s'" % user.email
    data = {
        'user': userid,
        'login': '/persona/login',
        'logout': '/persona/logout',
        'csrf_token': request.session.get_csrf_token(),
        'came_from': request.url,
        'request_params': request.registry['persona.request_params'],
    }
    return PERSONA_JS % data

def verify_persona_assertion(request):
    """Verify the assertion and the csrf token in the given request.

    Return the email of the user if everything is valid.

    otherwise raise HTTPBadRequest.
    """
    verifier = request.registry['persona.verifier']
    try:
        data = verifier.verify(request.POST['assertion'])
    except (ValueError, TrustError) as e:
        raise HTTPBadRequest('Invalid assertion')
    return data['email']

def persona_gravatar_photo(request, email):
    default = request.static_url('yss:static/persona.png')
    return ("http://www.gravatar.com/avatar/" +
            hashlib.md5(email.lower()).hexdigest() +
            "?" +
            urllib.urlencode({'d':default, 's': '40'})
           )

@view_config(
    name='login',
    route_name='persona',
    renderer='json',
    )
def persona_login(context, request):
    check_csrf_token(request)
    email = verify_persona_assertion(request)
    root = root_factory(request)
    adapter = request.registry.queryMultiAdapter(
        (root, request), IUserLocator)
    if adapter is None:
        adapter = DefaultUserLocator(root, request)
    user = adapter.get_user_by_email(email)
    if user is None:
        registry = request.registry
        username = 'persona:%s' % email
        principals = find_service(root, 'principals')
        user = principals.add_user(username, registry=registry)
        user.email = email
        performer = registry.content.create('Performer')
        root['performers'][username] = performer
        performer.user = user
        location = request.resource_url(performer, 'edit.html')
        performer.display_name = email
        performer.email = email
        performer.photo_url = persona_gravatar_photo(request, email)
        performer.age = colander.null
        performer.sex = user.favorite_genre = None
        location = request.resource_url(performer, 'edit.html')
    else:
        location = request.resource_url(root['performers'][user.__name__])
    headers = remember(request, get_oid(user))
    request.response.headers.extend(headers)
    return {'redirect': location, 'success': True}


@view_config(
    name='logout',
    route_name='persona',
    renderer='json',
    )
def persona_logout(context, request):
    """View to forget the user
    """
    request.response.headers.extend(forget(request))
    return {'redirect': request.resource_url(request.virtual_root)}


def authentication_type(request):
    if request.user is not None:
        if request.user.__name__.startswith('persona:'):
            return 'persona'
        return 'twitter'


@view_config(
    content_type='Song',
    name='mp3',
    #permission=???RETAIL_VIEW???
)
def stream_mp3(song, request):
    return FileResponse(
        song.blob.committed(),
        content_type='audio/mpeg')
