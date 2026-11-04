"""EPUB.js 0.3.93/1024 locations to Foliate CFI, bound to identical EPUB bytes.

EPUB.js and Foliate number mixed text/element children differently. Resolve
through each document's real text nodes rather than copying CFI strings.
No HTML, JavaScript, external entities or network resources are executed.
"""
import bisect
import hashlib
import math
import posixpath
import re
import zipfile
from xml.dom import minidom, Node

def length(text): return len(text.encode('utf-16-le'))//2
def children(node, kind): return [n for n in node.childNodes if n.nodeType == kind]
def elements(node): return children(node, Node.ELEMENT_NODE)

def parse_cfi(value, end=False):
    if not isinstance(value,str) or not value.startswith('epubcfi(') or not value.endswith(')'):
        raise ValueError('Invalid EPUB CFI')
    # Assertions do not change the indexed node; the complete source EPUB
    # hash is checked before every mapping. Correctly skip escaped brackets.
    out=[]; depth=0; escape=False
    for c in value[8:-1]:
        if escape: escape=False; continue
        if c=='^': escape=True; continue
        if c=='[': depth+=1; continue
        if c==']': depth-=1; continue
        if depth==0: out.append(c)
        if depth<0: raise ValueError('Invalid CFI assertion')
    if depth or escape: raise ValueError('Invalid CFI assertion')
    parts=''.join(out).split(',')
    if len(parts)==3: text=parts[0]+parts[2 if end else 1]
    elif len(parts)==1: text=parts[0]
    else: raise ValueError('Invalid CFI range')
    m=re.fullmatch(r'/\d+/(\d+)!((?:/\d+)+)(?::(\d+))?',text)
    if not m or int(m[1])<2 or int(m[1])%2: raise ValueError('Unsupported CFI structure')
    return int(m[1])//2-1,tuple(int(x) for x in m[2].split('/')[1:]),int(m[3] or 0)

def cfi(spine,path,offset,spine_step=6):
    return 'epubcfi(/'+str(spine_step)+'/'+str((spine+1)*2)+'!'+''.join('/'+str(x) for x in path)+':'+str(offset)+')'

def path_for(node, mode):
    path=[]; offset=0; current=node
    while current.parentNode and current.parentNode.nodeType!=Node.DOCUMENT_NODE:
        parent=current.parentNode
        if current.nodeType==Node.ELEMENT_NODE:
            step=(elements(parent).index(current)+1)*2
        elif current.nodeType==Node.TEXT_NODE:
            if mode=='jf': step=children(parent,Node.TEXT_NODE).index(current)*2+1
            else:
                count=0; chunk=0
                for x in parent.childNodes:
                    if x is current: break
                    if x.nodeType==Node.ELEMENT_NODE: count+=1;chunk=0
                    elif x.nodeType==Node.TEXT_NODE: chunk+=length(x.data)
                step=count*2+1;offset=chunk
        else: raise ValueError('Unsupported EPUB node')
        path.insert(0,step);current=parent
    return tuple(path),offset

def text_nodes(node):
    for child in node.childNodes:
        if child.nodeType==Node.CDATA_SECTION_NODE: raise ValueError('CDATA EPUB needs explicit reader validation')
        if child.nodeType==Node.TEXT_NODE: yield child
        elif child.nodeType==Node.ELEMENT_NODE: yield from text_nodes(child)

class EpubLocations:
    def __init__(self, filename, expected_hash=None):
        with open(filename,'rb') as f: self.hash=hashlib.sha256(f.read()).hexdigest()
        if expected_hash and self.hash!=expected_hash: raise ValueError('EPUB file changed')
        self.locations=[];self.nodes=[];self.element_paths=set();self.sizes=[];self.section_lengths=[]
        with zipfile.ZipFile(filename) as z:
            if sum(i.file_size for i in z.infolist())>128*1024*1024: raise ValueError('EPUB exceeds safe indexing limit')
            def xml(name):
                data=z.read(name)
                if len(data)>16*1024*1024 or b'<!ENTITY' in data: raise ValueError('Unsupported EPUB XML')
                return minidom.parseString(data)
            container=xml('META-INF/container.xml')
            roots=container.getElementsByTagNameNS('*','rootfile')
            if len(roots)!=1: raise ValueError('Ambiguous EPUB package')
            package=roots[0].getAttribute('full-path');doc=xml(package)
            spine=doc.getElementsByTagNameNS('*','spine')[0]
            self.spine_step=(elements(spine.parentNode).index(spine)+1)*2
            manifest={x.getAttribute('id'):x.getAttribute('href') for x in doc.getElementsByTagNameNS('*','item')}
            for index,ref in enumerate(elements(spine)):
                if ref.localName!='itemref': raise ValueError('Unexpected spine node')
                from urllib.parse import unquote
                name=posixpath.normpath(posixpath.join(posixpath.dirname(package),unquote(manifest[ref.getAttribute('idref')]).split('#')[0]))
                if name.startswith('../') or name.startswith('/'): raise ValueError('EPUB path outside package')
                linear=ref.getAttribute('linear')!='no';self.sizes.append(z.getinfo(name).file_size if linear else 0)
                section=xml(name);body=section.getElementsByTagNameNS('*','body')
                if len(body)!=1: raise ValueError('EPUB body missing')
                for el in [body[0],*body[0].getElementsByTagName('*')]:self.element_paths.add((index,path_for(el,'bo')[0]))
                nodes=list(text_nodes(body[0]));total=sum(length(n.data) for n in nodes);self.section_lengths.append(total)
                walked=0;counter=0;start=None;end=None
                for node in nodes:
                    ln=length(node.data);jp,_=path_for(node,'jf');bp,offset=path_for(node,'bo')
                    entry=dict(spine=index,jf=jp,bo=bp,offset=offset,length=ln,before=walked)
                    self.nodes.append(entry);walked+=ln
                    if not linear or not node.data.strip(): continue
                    pos=0
                    if counter==0: start=(index,jp,0,entry)
                    dist=1024-counter
                    if dist>ln: counter+=ln;pos=ln
                    while pos<ln:
                        dist=1024-counter
                        if counter==0: pos+=1;start=(index,jp,pos,entry)
                        if pos+dist>=ln: counter+=ln-pos;pos=ln
                        else:
                            pos+=dist;end=(index,jp,pos,entry)
                            self.locations.append((start,end));counter=0
                    end=(index,jp,ln,entry)
                if linear and start is not None and end is not None:self.locations.append((start,end))
        if len(self.locations)<2: raise ValueError('EPUB has insufficient locations')
        self.total=len(self.locations)-1
        self.keys=[self.key(x[0]) for x in self.locations]
        if self.keys!=sorted(self.keys): raise ValueError('EPUB.js locations are not ordered for this EPUB')

    @staticmethod
    def key(point):
        return point[0],tuple((x-1)//2 for x in point[1]),point[2]

    def bo_to_index(self,value):
        section,path,offset=parse_cfi(value)
        found=[n for n in self.nodes if n['spine']==section and n['bo']==path
               and n['offset']<=offset<=n['offset']+n['length']]
        if not found:
            if offset==0 and (section,path) in self.element_paths:
                descendant=next((n for n in self.nodes if n['spine']==section and n['bo'][:len(path)]==path),None)
                point=(section,descendant['jf'],0) if descendant else (section,path,0)
                return min(self.total,bisect.bisect_left(self.keys,self.key(point)))
            raise ValueError('BookOrbit text position cannot be resolved in EPUB')
        n=found[0];point=(section,n['jf'],offset-n['offset'])
        return min(self.total,bisect.bisect_left(self.keys,self.key(point)))

    def index_to_bo(self,index,at_end=False):
        if type(index) is not int or not 0<=index<=self.total: raise ValueError('Invalid EPUB location')
        section,path,offset,n=self.locations[index][1 if at_end else 0]
        point=cfi(section,n['bo'],n['offset']+offset,self.spine_step)
        size=sum(self.sizes)
        fraction=(n['before']+offset)/max(1,self.section_lengths[section])
        percentage=(sum(self.sizes[:section])+self.sizes[section]*fraction)/size*100
        return point,min(100,max(0,percentage))

    def ticks_to_index(self,ticks):
        if type(ticks) is not int or not 0<=ticks<=10000000: raise ValueError('Invalid Jellyfin reading position')
        # Server ticks are integer-rounded fractions. Avoid a spurious +1
        # location from rounding a fraction generated by this converter.
        value=self.total*ticks/10000000
        nearest=round(value)
        if abs(value-nearest)<=self.total/10000000: return min(self.total,nearest)
        return min(self.total,math.ceil(value))

    def index_to_ticks(self,index):
        # Floor is intentional: Jellyfin resumes with ceil(total*fraction).
        return math.floor(index/self.total*10000000)
