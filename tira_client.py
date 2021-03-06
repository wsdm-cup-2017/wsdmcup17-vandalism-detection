import sys
import re
import traceback

import cPickle
from StringIO import StringIO
from lxml import etree

from unicodecsv import reader

from twisted.protocols.basic import IntNStringReceiver
from twisted.internet.protocol import Protocol, ClientFactory
from twisted.internet import reactor

#import numpy as np
#import scipy.sparse as sp

import feature_extraction_utils as fu

### settings

args = sys.argv

dataserver = args[1] #'localhost:9901'
address, port = dataserver.split(':')
port = int(port)

access_token = args[2]


### the classifier

print 'reading the model...'
with open('model_vect.bin', 'rb') as f:
    text_vec = cPickle.load(f)

with open('model_svm.bin', 'rb') as f:
    svm = cPickle.load(f)


c = 0


def stringify_dict(d):
    dict_str = ' '.join('%s=%s' % (k, v.replace(' ', '_')) for (k, v) in d.items() if v)
    return dict_str

def ohe_featues(rev, meta):
    title = 'title=' + rev['page_title']

    ip_paths = fu.ip_path_features(rev['anonimous_ip'])
    meta_string = stringify_dict(meta)

    user_id = 'user_id=' + rev['user_id']
    user_string = user_id + ' ' + ip_paths + ' ' + meta_string
    
    comment = rev['revision_comment']
    struc = fu.extract_structured_comment(comment)
    links = fu.extract_links(comment)
    unstruc = fu.extract_unstructured_text(comment)
    comment_string = struc + ' ' + links + ' ' + unstruc

    all_features = title + ' ' + user_string + ' ' + comment_string

    return text_vec.transform([all_features])


def classifier(meta, rev):
    X = ohe_featues(rev, meta)
    scores = svm.decision_function(X)
    return scores[0]


### data parsing

re_page_title = re.compile('<title>(.+?)</title>')
re_page_id = re.compile('<id>(.+?)</id>')

def page_info(page_xml):
    page_res = {}
    page_res['page_title'] = re_page_title.findall(page_xml)[0]
    page_res['page_id'] = re_page_id.findall(page_xml)[0]
    return page_res

def text_or_none(el):
    if el is None:
        return None

    return el.text

def revision_info(rev_xml):
    rev_el = etree.fromstring(rev_xml)

    revision_res = {}
    revision_res['revision_id'] = text_or_none(rev_el.find('id'))

    contributor_el = rev_el.find('contributor')
    username = contributor_el.find('username')

    if username is not None:
        revision_res['username'] = username.text
        revision_res['user_id'] = text_or_none(contributor_el.find('id'))
        revision_res['anonimous_ip'] = None
    else:
        revision_res['username'] = None
        revision_res['user_id'] = '-1'
        revision_res['anonimous_ip'] = text_or_none(contributor_el.find('ip'))

    revision_res['revision_comment'] = text_or_none(rev_el.find('comment'))
    revision_res['revision_text'] = text_or_none(rev_el.find('text'))
    revision_res['revision_timestamp'] = text_or_none(rev_el.find('timestamp'))

    return revision_res


### parsing xml, socket reading & writing - technical stuff, not interesting

class EchoClient(IntNStringReceiver):
    MAX_LENGTH = 9999999
    structFormat = "!i"
    prefixLength = 4

    meta = None
    rev = None
    first = True

    def connectionMade(self):
        print 'writing access_token'
        self.write(access_token)

    def write(self, data):
        self.transport.write(data + '\r\n')

    def connectionLost(self, reason):
        print "connection lost:", reason
        print 'meta:', self.meta
        print 'rev:', self.rev

    def lengthLimitExceeded(self, length):
        self.transport.loseConnection()
        print >> sys.stderr, "the input is too large: %d" % length

    def stringReceived(self, data):
        if self.meta is None:
            self.meta = data
        elif self.rev is None:
            self.rev = data

            try:
                self.process_data(self.meta, self.rev)
                self.meta = None
                self.rev = None
            except Exception, e:
                self.transport.loseConnection()
                print >> sys.stderr, 'got exception in stringReceived:', e
                print 'got exception in stringReceived:', e
        else:
            print >> sys.stderr, 'Unexpected state: both meta and rev are not None'
            print 'Unexpected state: both meta and rev are not None'
            self.transport.loseConnection()

    def process_data(self, meta_in, rev_in):
        meta = self.try_decode(meta_in)
        rev = self.try_decode(rev_in)

        rev_id_back = self.safe_id_extract(meta, rev)

        try:
            if self.first:
                rev_id, score = self.process_data_first(meta, rev)
                self.write('REVISION_ID,VANDALISM_SCORE')
                self.first = False
            else:
                rev_id, score = self.process_data_others(meta, rev)

            if rev_id is not None:
                if int(rev_id) % 5000 == 0:
                    print 'processed rev_id =', rev_id
                self.write('%s,%f' % (rev_id, score))
            else:
                print >> sys.stderr, 'rev_id is None! something went wrong!'
                print >> sys.stderr, 'meta:', repr(meta)
                print >> sys.stderr, 'rev:', repr(rev)
                
                print 'rev_id is None! something went wrong!'
                print 'meta:', repr(meta)
                print 'rev:', repr(rev)

                if rev_id_back is not None:
                    print >> sys.stderr, 'cannot process rev %s, predicting 0 for it' % rev_id_back 
                    print 'cannot process rev %s, predicting 0 for it' % rev_id_back 

                    self.write('%s,%f' % (rev_id_back, 0))
                else:
                    print >> sys.stderr, 'cannot determine the revision id, stopping it' 
                    print 'cannot determine the revision id, stopping it' 
                    self.transport.loseConnection()

        except Exception, e:
            print 'got exception in process_data:', e
            traceback.print_exc()

            print 'meta:', meta
            print 'rev:', rev
            self.transport.loseConnection()
            raise e

    def safe_id_extract(self, meta, rev):
        if meta is not None:
            return meta.split(',')[0]
        if rev is not None:
            id_pattern = re.compile(r'<id>(\d+)</id>')
            ids = id_pattern.findall(rev)
            if ids:
                return ids[0]
        print >> sys.stderr, 'safe_id_extract: cannot find revision id'
        print 'safe_id_extract: cannot find revision id'
        return None

    def try_decode(self, str_in):
        try:
            res = str_in.decode('utf-8', 'ignore')
            return res.replace(u'\ufffd', '')
        except Exception, e:
            print >> sys.stderr, 'cannot decode', repr(str_in)
            print >> sys.stderr, 'decoding exception:', e
            print 'cannot decode', repr(str_in)
            print 'decoding exception:', e
            return None
        
    
    def process_data_first(self, meta, rev):
        r = reader(StringIO(meta))
        self.meta_header = next(r)
        meta_line = next(r)
        meta_rec = dict(zip(self.meta_header, meta_line))

        rev = rev[rev.find('<page>'):]
        page = rev[:rev.find('<revision>')]
        self.last_page_rec = page_info(page)

        rev_xml = rev[rev.find('<revision>'):]
        revision_rec = revision_info(rev_xml)
        revision_rec.update(self.last_page_rec)
        rev_id = revision_rec['revision_id']

        score = classifier(meta_rec, revision_rec)
        return rev_id, score


    def process_data_others(self, meta, rev):
        meta_rec = self.try_process_meta(meta)

        if '<page>' in rev and '</page>' in rev:
            rev = rev[rev.find('<page>'):]
            page = rev[:rev.find('<revision>')]
            self.last_page_rec = page_info(page)

        pos_rev_begin = rev.find('<revision>')
        pos_rev_end = rev.find('</revision>') + len('</revision>')
        rev_xml = rev[pos_rev_begin:pos_rev_end]

        revision_rec = revision_info(rev_xml)
        revision_rec.update(self.last_page_rec)
        rev_id = revision_rec['revision_id']

        score = classifier(meta_rec, revision_rec)
        return rev_id, score

    def try_process_meta(self, meta):
        try:
            meta_line = next(reader(StringIO(meta)))
            meta_rec = dict(zip(self.meta_header, meta_line))
            return meta_rec
        except Exception, e:
            print >> sys.stderr, 'cannot process meta information for some reason'
            print >> sys.stderr, e
            print 'cannot process meta information for some reason', e

            return {h: '' for h in self.meta_header}


class EchoFactory(ClientFactory):
    protocol = EchoClient

    def clientConnectionFailed(self, connector, reason):
        print "Connection failed - goodbye!", reason
        reactor.stop()

    def clientConnectionLost(self, connector, reason):
        print "Connection lost - goodbye!", reason
        reactor.stop()


factory = EchoFactory()
reactor.connectTCP(address, port, factory)
reactor.run()
