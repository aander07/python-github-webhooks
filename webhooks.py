#!/usr/bin/python
# -*- coding: utf-8 -*-
#
# Copyright (C) 2014, 2015, 2016 Carlos Jenkins <carlos@jenkins.co.cr>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""
Generic webhooks receiver for GitHub

Based on https://github.com/carlos-jenkins/python-github-webhooks
"""

import logging
from sys import stderr, hexversion

import hmac
import hashlib
from json import loads, dumps
from subprocess import Popen, PIPE
from tempfile import mkstemp
from os import access, X_OK, remove, fdopen
from os.path import isfile, abspath, normpath, dirname, join, basename

import requests
from ipaddress import ip_address, ip_network
from flask import Flask, request, abort

logging.basicConfig(stream=stderr)
application = Flask(__name__) # pylint: disable=invalid-name

class GithubMeta(object):
    """
    Cache the Github Meta data
    """

    def __init__(self):
        self._ips = []
        self._etag = ''

    @property
    def ips(self):
        """Return the IP list"""
        return self._ips

    @ips.setter
    def ips(self, val):
        self._ips = val

    @property
    def etag(self):
        """Return the eTag"""
        return self._etag

    @etag.setter
    def etag(self, val):
        self._etag = val


@application.route('/', methods=['GET', 'POST'])
def index():
    """
    Main WSGI application entry.
    """

    path = normpath(abspath(dirname(__file__)))

    # Only POST is implemented
    if request.method != 'POST':
        abort(501)

    # Load config
    with open(join(path, 'config.json'), 'r') as cfg:
        config = loads(cfg.read())

    hooks = config.get('hooks_path', join(path, 'hooks'))

    # Allow Github IPs only
    if config.get('github_ips_only', True):
        src_ip = ip_address(
            u'{}'.format(request.access_route[0])  # Fix stupid ipaddress issue
        )
        resp = requests.get(
            'https://api.github.com/meta',
            headers={'If-None-Match': ghm.etag}
        )
        if resp.status_code in [200]:
            ghm.etag = resp.headers['eTag']
            ghm.ips = resp.json()['hooks']
        elif resp.status_code not in [304]:
            abort(resp.status_code)

        whitelist = ghm.ips

        if config.get('allow_loopback', False) and u'127.0.0.0/8' not in whitelist:
            whitelist.append(u'127.0.0.0/8')
            ghm.ips = whitelist

        for valid_ip in whitelist:
            if src_ip in ip_network(valid_ip):
                break
        else:
            logging.error('IP {} not allowed {}'.format(
                src_ip,
                whitelist
            ))
            abort(403)

    # Enforce secret
    secret = config.get('enforce_secret', '')
    if secret:
        # Only SHA1 is supported
        header_signature = request.headers.get('X-Hub-Signature')
        if header_signature is None:
            abort(403)

        sha_name, signature = header_signature.split('=')
        if sha_name != 'sha1':
            abort(501)

        # HMAC requires the key to be bytes, but data is string
        mac = hmac.new(key=str(secret), msg=request.data, digestmod=hashlib.sha1)

        # Python prior to 2.7.7 does not have hmac.compare_digest
        if hexversion >= 0x020707F0:
            if not hmac.compare_digest(str(mac.hexdigest()), str(signature)):
                abort(403)
        else:
            # What compare_digest provides is protection against timing
            # attacks; we can live without this protection for a web-based
            # application
            if str(mac.hexdigest()) != str(signature):
                abort(403)

    # Implement ping
    event = request.headers.get('X-GitHub-Event', 'ping')
    if event == 'ping':
        return dumps({'msg': 'pong'})

    # Gather data
    try:
        payload = request.get_json()
    except Exception:
        logging.warning('Request parsing failed')
        abort(400)

    # Determining the branch is tricky, as it only appears for certain event
    # types an at different levels
    branch = None
    try:
        # Case 1: a ref_type indicates the type of ref.
        # This true for create and delete events.
        if 'ref_type' in payload:
            if payload['ref_type'] == 'branch':
                branch = payload['ref']

        # Case 2: a pull_request object is involved. This is pull_request and
        # pull_request_review_comment events.
        elif 'pull_request' in payload:
            # This is the TARGET branch for the pull-request, not the source
            # branch
            branch = payload['pull_request']['base']['ref']

        elif event in ['push']:
            # Push events provide a full Git ref in 'ref' and not a 'ref_type'.
            branch = payload['ref'].split('/', 2)[2]

    except KeyError:
        # If the payload structure isn't what we expect, we'll live without
        # the branch name
        pass

    # All current events have a repository, but some legacy events do not,
    # so let's be safe
    name = payload['repository']['name'] if 'repository' in payload else None

    meta = {
        'name': name,
        'branch': branch,
        'event': event
    }
    logging.info('Metadata:\n{}'.format(dumps(meta)))

    # Skip push-delete
    if event == 'push' and payload['deleted']:
        logging.info('Skipping push-delete event for {}'.format(dumps(meta)))
        return dumps({'status': 'skipped'})

    # Possible hooks
    scripts = []
    if branch and name:
        scripts.append(join(hooks, '{event}-{name}-{branch}'.format(**meta)))
    if name:
        scripts.append(join(hooks, '{event}-{name}'.format(**meta)))
    scripts.append(join(hooks, '{event}'.format(**meta)))
    scripts.append(join(hooks, 'all'))

    # Check permissions
    scripts = [s for s in scripts if isfile(s) and access(s, X_OK)]
    if not scripts:
        return dumps({'status': 'nop'})

    # Save payload to temporal file
    osfd, tmpfile = mkstemp()
    with fdopen(osfd, 'w') as payloadfile:
        payloadfile.write(dumps(payload))

    # Run scripts
    ran = {}
    for scr in scripts:

        proc = Popen(
            [scr, tmpfile, event],
            stdout=PIPE, stderr=PIPE
        )
        p_stdout, p_stderr = proc.communicate()

        ran[basename(scr)] = {
            'returncode': proc.returncode,
            'stdout': p_stdout.decode('utf-8'),
            'stderr': p_stderr.decode('utf-8'),
        }

        # Log errors if a hook failed
        if proc.returncode != 0:
            logging.error('{} : {} \n{}'.format(
                scr, proc.returncode, p_stderr
            ))

    # Remove temporal file
    remove(tmpfile)

    info = config.get('return_scripts_info', False)
    if not info:
        return dumps({'status': 'done'})

    output = dumps(ran, sort_keys=True, indent=4)
    logging.info(output)
    return output

@application.route('/check', methods=['GET', 'HEAD'])
def check():
    """
    HAproxy check
    """
    # Only GET is implemented
    if request.method not in ['GET', 'HEAD']:
        abort(501)

    return 'ok'

if __name__ == '__main__':
    ghm = GithubMeta()
    application.run(debug=True, host='0.0.0.0', port=6000)
