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
from Products.Archetypes.interfaces.base import IBaseFolder
import transaction
from zope.app.component.hooks import getSite
from wildcard.migrator.content import ContentTouchMigrator
from Products.CMFCore.utils import getToolByName
from wildcard.migrator.content import resolveuid_re

from wildcard.migrator import scan
scan()


def replaceUids(data):
    pass


class ContentMigrator(object):

    def __init__(self, req, source, site, threshold=150):
        self.req = req
        self.resp = req.response
        self.threshold = threshold
        self.source = source
        self.site = site
        self.count = 0
        self.imported = []
        self.stubs = []
        self.convertedUids = {}  # a mapping of old site uid, to new site uid
        self.uid_cat = getToolByName(self.site, 'uid_catalog')
        self.sitepath = '/'.join(getSite().getPhysicalPath())

    def _fixUids(self, value):
        """ must be a string argument"""
        if value.startswith(json._uid_marker):
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
        elif type(data) in (list, tuple, set):
            for idx, v in enumerate(data):
                if isinstance(v, basestring):
                    data[idx] = self._fixUids(v)
                elif type(v) in (dict, list, tuple, set):
                    self.convertUids(v)

    def _touchPath(self, path):
        # have data for request but no object created yet?
        # need to assemble obj first then
        path = path.lstrip('/')
        obj = self.site.restrictedTraverse(path, None)
        if obj:
            return obj
        resp = requests.post(self.source, data={
            'migrator': ContentTouchMigrator.title,
            'context': '_',
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

    def migrateObject(self, obj):
        objpath = '/'.join(obj.getPhysicalPath())[len(self.sitepath) + 1:]
        if objpath not in self.imported:
            response = requests.post(self.source, data={
                'migrator': ContentObjectMigrator.title,
                'context': 'object',
                'path': objpath
            })
            content = json.loads(response.content)
            for uid, path in content['uids']:
                if uid not in self.convertedUids:
                    path = str(path)
                    if path in self.stubs or path in self.imported:
                        uidObj = self.site.restrictedTraverse(path)
                        self.convertedUids[uid] = uidObj.UID()
                    else:
                        # create stub object if they aren't there
                        # this is so we can convert uids
                        self.touchPath(path, uid)

            self.convertUids(content)
            migr = ContentObjectMigrator(self.site, obj)
            error = True
            while error:
                error = False
                try:
                    migr.set(content)
                except MissingObjectException, ex:
                    self._touchPath(ex.path)
                    error = True

            self.imported.append(objpath)

            self.count += 1
            self.resp.write('%i: updating object %s\n' % (self.count,
                '/'.join(obj.getPhysicalPath())))
            if self.count % self.threshold == 0:
                transaction.commit()

            obj.reindexObject()
        if IBaseFolder.providedBy(obj):
            migr = FolderContentsMigrator(self.site, obj)
            folderdata = requests.post(self.source, data={
                'migrator': FolderContentsMigrator.title,
                'context': 'folder',
                'path': '/'.join(obj.getPhysicalPath()
                    )[len(self.sitepath) + 1:]
            })
            self(migr, json.loads(folderdata.content))

    def __call__(self, migrator, data):
        for obj in migrator.set(data):
            self.migrateObject(obj)
        return self.count


class Importer(BrowserView):
    template = ViewPageTemplateFile("importer.pt")

    def site_migrators(self):
        return getMigratorsOfType('site')

    def __call__(self):
        if self.request.get('REQUEST_METHOD') == 'POST':
            source = self.request.get('source')
            if not source:
                raise Exception("Must specify a source")
            migratorname = self.request.get('migrator')
            migrator = getMigratorFromRequest(self.request)
            source = source.rstrip('/') + '/@@migrator-exporter'
            result = requests.post(source, data={
                'migrator': self.request.get('migrator'),
                'context': 'site'})
            data = json.loads(result.content)
            if migratorname == SiteContentsMigrator.title:
                threshold = int(self.request.get('threshold', '150'))
                contentmigrator = ContentMigrator(self.request,
                    source, migrator.site, threshold)
                contentmigrator(migrator, data)
                return ''
            else:
                migrator.set(data)
        return self.template()