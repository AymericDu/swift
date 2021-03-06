# Copyright (c) 2010-2020 OpenStack Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from base64 import standard_b64encode as b64encode
from base64 import standard_b64decode as b64decode

import six
from six.moves.urllib.parse import quote

from swift.common import swob
from swift.common.http import HTTP_OK
from swift.common.middleware.versioned_writes.object_versioning import \
    DELETE_MARKER_CONTENT_TYPE
from swift.common.utils import json, public, config_true_value, Timestamp, \
    get_swift_info

from swift.common.middleware.s3api.controllers.base import Controller, \
    log_s3api_command
from swift.common.middleware.s3api.controllers.cors import \
    CORS_ALLOWED_HTTP_METHOD, cors_fill_headers, get_cors
from swift.common.middleware.s3api.etree import Element, SubElement, \
    tostring, fromstring, XMLSyntaxError, DocumentInvalid
from swift.common.middleware.s3api.iam import check_iam_access
from swift.common.middleware.s3api.s3response import \
    HTTPOk, S3NotImplemented, InvalidArgument, \
    MalformedXML, InvalidLocationConstraint, NoSuchBucket, \
    BucketNotEmpty, InternalError, ServiceUnavailable, NoSuchKey, \
    CORSForbidden, CORSInvalidAccessControlRequest, CORSOriginMissing
from swift.common.middleware.s3api.utils import MULTIUPLOAD_SUFFIX

MAX_PUT_BUCKET_BODY_SIZE = 10240


class BucketController(Controller):
    """
    Handles bucket request.
    """
    def _delete_segments_bucket(self, req):
        """
        Before delete bucket, delete segments bucket if existing.
        """
        container = req.container_name + MULTIUPLOAD_SUFFIX
        marker = ''
        seg = ''

        try:
            resp = req.get_response(self.app, 'HEAD')
            if int(resp.sw_headers['X-Container-Object-Count']) > 0:
                raise BucketNotEmpty()
            # FIXME: This extra HEAD saves unexpected segment deletion
            # but if a complete multipart upload happen while cleanup
            # segment container below, completed object may be missing its
            # segments unfortunately. To be safer, it might be good
            # to handle if the segments can be deleted for each object.
        except NoSuchBucket:
            pass

        try:
            while True:
                # delete all segments
                resp = req.get_response(self.app, 'GET', container,
                                        query={'format': 'json',
                                               'marker': marker})
                segments = json.loads(resp.body)
                for seg in segments:
                    try:
                        req.get_response(
                            self.app, 'DELETE', container,
                            swob.bytes_to_wsgi(seg['name'].encode('utf8')))
                    except NoSuchKey:
                        pass
                    except InternalError:
                        raise ServiceUnavailable()
                if segments:
                    marker = seg['name']
                else:
                    break
            req.get_response(self.app, 'DELETE', container)
        except NoSuchBucket:
            return
        except (BucketNotEmpty, InternalError):
            raise ServiceUnavailable()

    @public
    @check_iam_access("s3:ListBucket")
    def HEAD(self, req):
        """
        Handle HEAD Bucket (Get Metadata) request
        """
        self.set_s3api_command(req, 'head-bucket')

        resp = req.get_response(self.app)

        return HTTPOk(headers=resp.headers)

    def _parse_request_options(self, req, max_keys):
        encoding_type = req.params.get('encoding-type')
        if encoding_type is not None and encoding_type != 'url':
            err_msg = 'Invalid Encoding Method specified in Request'
            raise InvalidArgument('encoding-type', encoding_type, err_msg)

        # in order to judge that truncated is valid, check whether
        # max_keys + 1 th element exists in swift.
        query = {
            'limit': max_keys + 1,
        }
        if 'prefix' in req.params:
            query['prefix'] = req.params['prefix']
        if 'delimiter' in req.params:
            query['delimiter'] = req.params['delimiter']
        fetch_owner = False
        if 'versions' in req.params:
            query['versions'] = req.params['versions']
            listing_type = 'object-versions'
            if 'key-marker' in req.params:
                query['marker'] = req.params['key-marker']
                version_marker = req.params.get('version-id-marker')
                if version_marker is not None:
                    if version_marker != 'null':
                        try:
                            Timestamp(version_marker)
                        except ValueError:
                            raise InvalidArgument(
                                'version-id-marker',
                                req.params['version-id-marker'],
                                'Invalid version id specified')
                    query['version_marker'] = version_marker
            elif 'version-id-marker' in req.params:
                err_msg = ('A version-id marker cannot be specified without '
                           'a key marker.')
                raise InvalidArgument('version-id-marker',
                                      req.params['version-id-marker'], err_msg)
        elif int(req.params.get('list-type', '1')) == 2:
            listing_type = 'version-2'
            if 'start-after' in req.params:
                query['marker'] = req.params['start-after']
            # continuation-token overrides start-after
            if 'continuation-token' in req.params:
                decoded = b64decode(req.params['continuation-token'])
                if not six.PY2:
                    decoded = decoded.decode('utf8')
                query['marker'] = decoded
            if 'fetch-owner' in req.params:
                fetch_owner = config_true_value(req.params['fetch-owner'])
        else:
            listing_type = 'version-1'
            if 'marker' in req.params:
                query['marker'] = req.params['marker']

        return encoding_type, query, listing_type, fetch_owner

    def _build_versions_result(self, req, objects, is_truncated):
        elem = Element('ListVersionsResult')
        SubElement(elem, 'Name').text = req.container_name
        SubElement(elem, 'Prefix').text = req.params.get('prefix')
        SubElement(elem, 'KeyMarker').text = req.params.get('key-marker')
        SubElement(elem, 'VersionIdMarker').text = req.params.get(
            'version-id-marker')
        if is_truncated:
            if 'name' in objects[-1]:
                SubElement(elem, 'NextKeyMarker').text = \
                    objects[-1]['name']
                SubElement(elem, 'NextVersionIdMarker').text = \
                    objects[-1].get('version') or 'null'
            if 'subdir' in objects[-1]:
                SubElement(elem, 'NextKeyMarker').text = \
                    objects[-1]['subdir']
                SubElement(elem, 'NextVersionIdMarker').text = 'null'
        return elem

    def _build_base_listing_element(self, req):
        elem = Element('ListBucketResult')
        SubElement(elem, 'Name').text = req.container_name
        SubElement(elem, 'Prefix').text = req.params.get('prefix')
        return elem

    def _build_list_bucket_result_type_one(self, req, objects, encoding_type,
                                           is_truncated):
        elem = self._build_base_listing_element(req)
        SubElement(elem, 'Marker').text = req.params.get('marker')
        if is_truncated and 'delimiter' in req.params:
            if 'name' in objects[-1]:
                name = objects[-1]['name']
            else:
                name = objects[-1]['subdir']
            if encoding_type == 'url':
                name = quote(name.encode('utf-8'))
            SubElement(elem, 'NextMarker').text = name
        # XXX: really? no NextMarker when no delimiter??
        return elem

    def _build_list_bucket_result_type_two(self, req, objects, is_truncated):
        elem = self._build_base_listing_element(req)
        if is_truncated:
            if 'name' in objects[-1]:
                SubElement(elem, 'NextContinuationToken').text = \
                    b64encode(objects[-1]['name'].encode('utf8'))
            if 'subdir' in objects[-1]:
                SubElement(elem, 'NextContinuationToken').text = \
                    b64encode(objects[-1]['subdir'].encode('utf8'))
        if 'continuation-token' in req.params:
            SubElement(elem, 'ContinuationToken').text = \
                req.params['continuation-token']
        if 'start-after' in req.params:
            SubElement(elem, 'StartAfter').text = \
                req.params['start-after']
        SubElement(elem, 'KeyCount').text = str(len(objects))
        return elem

    def _finish_result(self, req, elem, tag_max_keys, encoding_type,
                       is_truncated):
        SubElement(elem, 'MaxKeys').text = str(tag_max_keys)
        if 'delimiter' in req.params:
            SubElement(elem, 'Delimiter').text = req.params['delimiter']
        if encoding_type == 'url':
            SubElement(elem, 'EncodingType').text = encoding_type
        SubElement(elem, 'IsTruncated').text = \
            'true' if is_truncated else 'false'

    def _add_subdir(self, elem, o, encoding_type):
        common_prefixes = SubElement(elem, 'CommonPrefixes')
        name = o['subdir']
        if encoding_type == 'url':
            name = quote(name.encode('utf-8'))
        SubElement(common_prefixes, 'Prefix').text = name

    def _add_object(self, req, elem, o, encoding_type, listing_type,
                    fetch_owner):
        name = o['name']
        if encoding_type == 'url':
            name = quote(name.encode('utf-8'))

        if listing_type == 'object-versions':
            if o['content_type'] == DELETE_MARKER_CONTENT_TYPE:
                contents = SubElement(elem, 'DeleteMarker')
            else:
                contents = SubElement(elem, 'Version')
            SubElement(contents, 'Key').text = name
            SubElement(contents, 'VersionId').text = o.get(
                'version_id') or 'null'
            if 'object_versioning' in get_swift_info():
                SubElement(contents, 'IsLatest').text = (
                    'true' if o['is_latest'] else 'false')
            else:
                SubElement(contents, 'IsLatest').text = 'true'
        else:
            contents = SubElement(elem, 'Contents')
            SubElement(contents, 'Key').text = name
        SubElement(contents, 'LastModified').text = \
            o['last_modified'][:-3] + 'Z'
        if contents.tag != 'DeleteMarker':
            if 's3_etag' in o:
                # New-enough MUs are already in the right format
                etag = o['s3_etag']
            elif 'slo_etag' in o:
                # SLOs may be in something *close* to the MU format
                etag = '"%s-N"' % o['slo_etag'].strip('"')
            else:
                # Normal objects just use the MD5
                etag = o['hash']
                if len(etag) < 2 or etag[::len(etag) - 1] != '""':
                    # Normal objects just use the MD5
                    etag = '"%s"' % o['hash']
                    # This also catches sufficiently-old SLOs, but we have
                    # no way to identify those from container listings
                # Otherwise, somebody somewhere (proxyfs, maybe?) made this
                # look like an RFC-compliant ETag; we don't need to
                # quote-wrap.
            SubElement(contents, 'ETag').text = etag
            SubElement(contents, 'Size').text = str(o['bytes'])
        if fetch_owner or listing_type != 'version-2':
            owner = SubElement(contents, 'Owner')
            SubElement(owner, 'ID').text = req.user_id
            SubElement(owner, 'DisplayName').text = req.user_id
        if contents.tag != 'DeleteMarker':
            SubElement(contents, 'StorageClass').text = 'STANDARD'

    def _add_objects_to_result(self, req, elem, objects, encoding_type,
                               listing_type, fetch_owner):
        for o in objects:
            if 'subdir' in o:
                self._add_subdir(elem, o, encoding_type)
            else:
                self._add_object(req, elem, o, encoding_type, listing_type,
                                 fetch_owner)

    @public
    @check_iam_access("s3:ListBucket")
    def GET(self, req):
        """
        Handle GET Bucket (List Objects) request
        """
        max_keys = req.get_validated_param(
            'max-keys', self.conf.max_bucket_listing)
        tag_max_keys = max_keys
        # TODO: Separate max_bucket_listing and default_bucket_listing
        max_keys = min(max_keys, self.conf.max_bucket_listing)

        encoding_type, query, listing_type, fetch_owner = \
            self._parse_request_options(req, max_keys)

        if listing_type == 'object-versions':
            self.set_s3api_command(req, 'list-object-versions')
        elif listing_type == 'version-2':
            self.set_s3api_command(req, 'list-objects-v2')
        else:
            self.set_s3api_command(req, 'list-objects')

        query['format'] = 'json'
        resp = req.get_response(self.app, query=query)

        objects = json.loads(resp.body)

        is_truncated = max_keys > 0 and len(objects) > max_keys
        objects = objects[:max_keys]

        if listing_type == 'object-versions':
            elem = self._build_versions_result(req, objects, is_truncated)
        elif listing_type == 'version-2':
            elem = self._build_list_bucket_result_type_two(
                req, objects, is_truncated)
        else:
            elem = self._build_list_bucket_result_type_one(
                req, objects, encoding_type, is_truncated)
        self._finish_result(
            req, elem, tag_max_keys, encoding_type, is_truncated)
        self._add_objects_to_result(
            req, elem, objects, encoding_type, listing_type, fetch_owner)

        body = tostring(elem)
        resp = HTTPOk(body=body, content_type='application/xml')

        origin = req.headers.get('Origin')
        if origin:
            rule = get_cors(self.app, req, "GET", origin)
            if rule:
                cors_fill_headers(req, resp, rule)

        return resp

    @public
    @check_iam_access("s3:CreateBucket")
    def PUT(self, req):
        """
        Handle PUT Bucket request
        """
        self.set_s3api_command(req, 'create-bucket')

        xml = req.xml(MAX_PUT_BUCKET_BODY_SIZE)
        if xml:
            # check location
            try:
                elem = fromstring(
                    xml, 'CreateBucketConfiguration', self.logger)
                location = elem.find('./LocationConstraint').text
            except (XMLSyntaxError, DocumentInvalid):
                raise MalformedXML()
            except Exception as e:
                self.logger.error(e)
                raise

            if location != self.conf.location:
                # s3api cannot support multiple regions currently.
                raise InvalidLocationConstraint()

        resp = req.get_response(self.app)

        resp.status = HTTP_OK
        resp.location = '/' + req.container_name

        return resp

    @public
    @check_iam_access("s3:DeleteBucket")
    def DELETE(self, req):
        """
        Handle DELETE Bucket request
        """
        self.set_s3api_command(req, 'delete-bucket')

        # NB: object_versioning is responsible for cleaning up its container
        if self.conf.allow_multipart_uploads:
            self._delete_segments_bucket(req)
        resp = req.get_response(self.app)
        return resp

    @public
    def POST(self, req):
        """
        Handle POST Bucket request
        """
        raise S3NotImplemented()

    @public
    @log_s3api_command('options')
    def OPTIONS(self, req):
        origin = req.headers.get('Origin')
        if not origin:
            raise CORSOriginMissing()

        method = req.headers.get('Access-Control-Request-Method')
        if method not in CORS_ALLOWED_HTTP_METHOD:
            raise CORSInvalidAccessControlRequest(method=method)

        rule = get_cors(self.app, req, method, origin)
        # FIXME(mbo): we should raise also NoSuchCORSConfiguration
        if rule is None:
            raise CORSForbidden(method)

        resp = HTTPOk(body=None)
        del resp.headers['Content-Type']

        return cors_fill_headers(req, resp, rule)
