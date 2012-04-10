from wildcard.migrator.exceptions import MissingObjectException
from wildcard.migrator import getMigratorsOfType
from Products.Five.browser.pagetemplatefile import ViewPageTemplateFile
from wildcard.migrator.utils import getMigratorFromRequest
from Products.Five import BrowserView
import requests
from wildcard.migrator import mjson as json
from wildcard.migrator.content import SiteContentsMigrator
from wildcard.migrator.content import FolderContentsMigrator
from wildcard.migrator.content import ContentObjectMigrator
from wildcard.migrator.archetypes import FieldMigrator
from Products.Archetypes.interfaces.base import IBaseFolder
import transaction
from zope.app.component.hooks import getSite
from wildcard.migrator.content import ContentTouchMigrator
from wildcard.migrator.content import MultiContentTouchMigrator
from Products.CMFCore.utils import getToolByName
from wildcard.migrator.content import resolveuid_re
from wildcard.migrator.utils import safeTraverse
from plone.app.blob.interfaces import IBlobField
from StringIO import StringIO
from wildcard.migrator import scan
from persistent.list import PersistentList
scan()

import logging
logger = logging.getLogger('wildcard.migrator')


# some ids need to be converted...
# when this is done, we also need to add a redirect
# for the old path
_id_conversions = {
    'images': 'Images'
}


class ContentMigrator(object):

    def __init__(self, req, source, sourcesite, site,
                 threshold=150, attributes=[], onlyNew=False, index=False):
        self.req = req
        self.resp = req.response
        self.threshold = threshold
        self.source = source
        self.sourcesite = sourcesite
        self.site = site
        self.count = 0
        self.imported = []
        self.stubs = []
        self.convertedUids = {}  # a mapping of old site uid, to new site uid
        self.uid_cat = getToolByName(self.site, 'uid_catalog')
        self.sitepath = '/'.join(getSite().getPhysicalPath())
        self.attributes = attributes
        self.onlyNew = onlyNew
        self.index = index

    def _fixUids(self, value):
        """ must be a string argument"""
        if value.startswith(json._filedata_marker):
            # do not check file data
            return value
        elif value.startswith(json._uid_marker):
            # converted uid
            # these may need to be touched
            uid, path = json.decodeUid(value)
            if uid not in self.convertedUids:
                _, realuid, _ = self.touchPath(path, uid)
            else:
                realuid = self.convertedUids[uid]
            return self.uid_cat(UID=realuid)[0].getObject()
        elif 'resolveuid/' in value:
            for uid in resolveuid_re.findall(value):
                if uid in self.convertedUids:
                    value = value.replace('resolveuid/%s' % uid,
                        'resolveuid/%s' % self.convertedUids[uid])
        return value

    def convertUids(self, data):
        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, basestring):
                    data[key] = self._fixUids(value)
                elif type(value) in (list, tuple, set):
                    for idx, v in enumerate(value):
                        if isinstance(v, basestring):
                            value[idx] = self._fixUids(v)
                        elif type(v) in (dict, list, tuple, set):
                            self.convertUids(v)
                elif type(value) == dict:
                    self.convertUids(value)
        elif type(data) in (list, tuple):
            for idx, v in enumerate(data):
                if isinstance(v, basestring):
                    data[idx] = self._fixUids(v)
                elif type(v) in (dict, list, tuple, set):
                    self.convertUids(v)

    def _touchPath(self, path):
        # have data for request but no object created yet?
        # need to assemble obj first then
        path = str(path.lstrip('/'))
        obj = safeTraverse(self.site, path, None)
        if obj:
            return obj
        resp = requests.post(self.source, data={
            'migrator': ContentTouchMigrator.title,
            'path': path
        })
        content = json.loads(resp.content)
        migr = ContentTouchMigrator(self.site, path)
        return migr.set(content)

    def touchPath(self, path, uid):
        touched = self._touchPath(path)
        self.stubs.append(path)
        realuid = touched.UID()
        self.convertedUids[uid] = realuid
        return touched, realuid, uid

    def touchPaths(self, uids):
        totouch = []
        for path, uid in uids:
            path = str(path.lstrip('/'))
            obj = safeTraverse(self.site, path, None)
            if not obj:
                totouch.append((path, uid))
            else:
                self.convertedUids[uid] = obj.UID()
        resp = requests.post(self.source, data={
            'migrator': MultiContentTouchMigrator.title,
            'args': json.dumps({'totouch': totouch})
        })
        content = json.loads(resp.content)
        migr = MultiContentTouchMigrator(self.site)
        for touched, olduid in migr.set(content):
            path = '/'.join(touched.getPhysicalPath())[len(self.sitepath) + 1:]
            self.stubs.append(path.lstrip('/'))
            realuid = touched.UID()
            self.convertedUids[olduid] = realuid

    def handleDeferred(self, obj, objpath, content):
        """
        very large files get deferred to get sent out
        so we need an extra handler for it
        """
        if 'fieldvalues' in content:
            for fieldname, value in content['fieldvalues'].items():
                if value['value'] == json.Deferred:
                    resp = requests.post(self.sourcesite + \
                            '/@@migrator-exportfield',
                        data={'path': objpath, 'field': fieldname})
                    largefile = False
                    if int(resp.headers['content-length']) / 1024 / 1024 > 50:
                        largefile = True
                        transaction.commit()  # commit before and after
                    migr = FieldMigrator(self.site, obj, fieldname)
                    field = obj.getField(fieldname)
                    content = resp.content
                    filename = resp.headers.get('filename', '')
                    mimetype = resp.headers.get('content-type', '')
                    if IBlobField.providedBy(field):
                        # not a blob field here...
                        content = StringIO(content)
                        content.filename = filename
                    migr.set({'value': content, 'extras': {
                        'filename': filename,
                        'mimetype': mimetype
                    }})
                    if largefile:
                        transaction.commit()

    def migrateObject(self, obj):
        objpath = '/'.join(obj.getPhysicalPath())[len(self.sitepath) + 1:]
        if objpath not in self.imported and not \
                (self.onlyNew and objpath in self.site._import_results):
            response = requests.post(self.source, data={
                'migrator': ContentObjectMigrator.title,
                'path': objpath,
                'args': json.dumps({
                    'attributes': self.attributes})
            })
            content = json.loads(response.content)
            totouch = []
            for uid, path in content['uids']:
                if uid not in self.convertedUids:
                    path = str(path).lstrip('/')
                    uidObj = None
                    if path in self.stubs or path in self.imported:
                        uidObj = safeTraverse(self.site, path, None)
                        if uidObj:
                            self.convertedUids[uid] = uidObj.UID()
                    if uidObj is None:
                        # create stub object if they aren't there
                        # this is so we can convert uids
                        totouch.append((path, uid))
                        #self.touchPath(path, uid)
            if totouch:
                self.touchPaths(totouch)

            self.convertUids(content)
            logger.info('apply data migrations on %s' % (
                '/'.join(obj.getPhysicalPath())))
            migr = ContentObjectMigrator(self.site, obj)
            error = True
            while error:
                error = False
                try:
                    migr.set(content)
                    self.handleDeferred(obj, objpath, content)
                except MissingObjectException, ex:
                    logger.info(
                        'oops, could not find %s - touching' % ex.path)
                    try:
                        self._touchPath(ex.path)
                    except ValueError:
                        # error in response. must not be valid object
                        pass
                    error = True

            self.imported.append(objpath)

            self.count += 1
            self.resp.write('%i: updating object %s\n' % (self.count,
                '/'.join(obj.getPhysicalPath())))
            if objpath not in self.site._import_results:
                self.site._import_results.append(objpath)

            if self.count % self.threshold == 0:
                transaction.commit()

            if self.index:
                obj.reindexObject()
            logger.info('finished migrating %s' % (
                '/'.join(obj.getPhysicalPath())))
        if IBaseFolder.providedBy(obj):
            migr = FolderContentsMigrator(self.site, obj)
            folderdata = requests.post(self.source, data={
                'migrator': FolderContentsMigrator.title,
                'path': '/'.join(obj.getPhysicalPath()
                    )[len(self.sitepath) + 1:]
            })
            self(migr, json.loads(folderdata.content))

    def __call__(self, migrator, data):
        for obj in migrator.set(data):
            logger.info('start migrating %s' % '/'.join(obj.getPhysicalPath()))
            self.migrateObject(obj)
        return self.count


class Importer(BrowserView):
    template = ViewPageTemplateFile("importer.pt")

    def site_migrators(self):
        return getMigratorsOfType('site')

    def __call__(self):
        if not hasattr(self.context, '_import_results'):
            self.context._import_results = PersistentList()
        if self.request.get('REQUEST_METHOD') == 'POST':
            sourcesite = self.request.get('source', '').rstrip('/')
            if not sourcesite:
                raise Exception("Must specify a source")
            migratorname = self.request.get('migrator')
            migrator = getMigratorFromRequest(self.request)
            source = sourcesite + '/@@migrator-exporter'
            attributes = self.request.get('attributes', '').splitlines()
            result = requests.post(source, data={
                'migrator': self.request.get('migrator')})
            data = json.loads(result.content)
            if self.request.get('onlyNew', False):
                onlyNew = True
            else:
                onlyNew = False
            if self.request.get('index', False):
                index = True
            else:
                index = False
            if migratorname == SiteContentsMigrator.title:
                threshold = int(self.request.get('threshold', '150'))
                contentmigrator = ContentMigrator(self.request, source,
                    sourcesite, migrator.site,
                    threshold=threshold,
                    attributes=attributes,
                    onlyNew=onlyNew,
                    index=index)
                contentmigrator(migrator, data)
            else:
                migrator.set(data)
            return 'done'
        return self.template()


class ImportObject(BrowserView):

    def __call__(self):
        sourcesite = self.request.get('source', '').rstrip('/')
        if not sourcesite:
            raise Exception("Must specify a source")
        source = sourcesite + '/@@migrator-exporter'
        threshold = int(self.request.get('threshold', '150'))
        contentmigrator = ContentMigrator(self.request,
            source, sourcesite, getSite(), threshold)
        path = self.request.get('path')
        obj = contentmigrator._touchPath(path)
        contentmigrator.migrateObject(obj)
        return ''
