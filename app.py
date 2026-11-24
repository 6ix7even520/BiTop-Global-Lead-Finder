from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.request import HTTPRedirectHandler, build_opener
from pathlib import Path
from html.parser import HTMLParser
from html import escape as html_escape
from contextlib import contextmanager
from urllib.parse import urlsplit, urljoin, urlencode
from urllib import robotparser
import sqlite3, json, urllib, urllib.parse, urllib.request, urllib.error, csv, io, webbrowser, threading, os, sys, re, socket, ipaddress, time
from datetime import datetime
from siwc import (status as siwc_status, start_signin, complete_callback, list_models, set_selected_model, responses_text, get_access_token, disconnect as siwc_disconnect, load_settings, save_settings)

ROOT = Path(__file__).resolve().parent
DB = ROOT / 'leads.sqlite3'
USER_AGENT = 'BiTopLeadResearch/1.2.1 (+public business information research)'
LOCAL_HOST = '127.0.0.1:8765'
LOCAL_ORIGIN = 'http://127.0.0.1:8765'
MAX_BODY = 2_000_000
FIELDS = ['name','company','country','product','channel','url','website','snippet','brands','contact','contact_source','evidence_url','notes','status','match','identity','b2b','contact_ok','local','competitor','ai_report','research_at','research_confidence']
FLAGS = ['match','identity','b2b','contact_ok','local','competitor']
COUNTRIES = {
    'PH': ('Philippines','en'), 'ID': ('Indonesia','id'), 'VN': ('Vietnam','vi'),
    'TH': ('Thailand','th'), 'MY': ('Malaysia','ms'), 'SG': ('Singapore','en'),
    'KH': ('Cambodia','en'), 'TN': ('Tunisia','fr')
}
# Public marketplace/social pages are retained as search evidence but not automatically fetched.
SKIP_FETCH = ('shopee.','lazada.','tokopedia.','tiktok.','facebook.','instagram.','linkedin.','youtube.','google.','bing.','x.com','twitter.com','amazon.')
CONTACT_EMAIL_RE = re.compile(r'(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b')
CONTACT_PHONE_RE = re.compile(r'(?<!\w)(?:\+\s?\d{1,3}[\s().-]?)?(?:\(?\d{2,4}\)?[\s().-]?)?\d{3,4}[\s().-]?\d{3,4}(?!\w)')


def validate_local_host(headers):
    host = str(headers.get('Host', '')).strip().lower()
    if host != LOCAL_HOST:
        raise ValueError('请使用 http://127.0.0.1:8765 访问本地系统')


def validate_local_request(headers, require_origin=False):
    validate_local_host(headers)
    origin = headers.get('Origin')
    if require_origin and origin != LOCAL_ORIGIN:
        raise ValueError('请求来源无效，请从本地 BiTop 系统页面操作')
    if not require_origin and origin not in (None, LOCAL_ORIGIN):
        raise ValueError('已拒绝跨站本地请求')


@contextmanager
def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def init():
    with db() as c:
        c.execute('CREATE TABLE IF NOT EXISTS leads(id INTEGER PRIMARY KEY, created TEXT, updated TEXT, ' + ','.join(x + (' INTEGER DEFAULT 0' if x in FLAGS else " TEXT DEFAULT ''") for x in FIELDS) + ')')
        # Backward-compatible migration from V1.0 databases.
        existing = {r['name'] for r in c.execute('PRAGMA table_info(leads)')}
        for x in FIELDS:
            if x not in existing:
                c.execute('ALTER TABLE leads ADD COLUMN ' + x + (' INTEGER DEFAULT 0' if x in FLAGS else " TEXT DEFAULT ''"))


def clean(d):
    out = {}
    for k in FIELDS:
        if k in FLAGS:
            out[k] = int(d.get(k) in (True, 1, '1', 'true', 'yes'))
        else:
            val = str(d.get(k, '') or '').strip()
            out[k] = val[:100000 if k == 'ai_report' else 20000]
    return out


def grade(d):
    score = 25*d['match'] + 25*d['identity'] + 25*d['b2b'] + 15*d['contact_ok'] + 10*d['local']
    g = 'C' if d['competitor'] else 'A' if all(d[x] for x in ['match','identity','b2b','contact_ok']) else 'B' if d['match'] else '待核实'
    return score, g


def canon(url):
    try:
        p = urlsplit((url or '').strip())
        return (p.netloc.lower().removeprefix('www.') + p.path.rstrip('/') + ('?' + p.query if p.query else '')).lower() if p.scheme in ('http','https') and p.netloc else ''
    except ValueError:
        return ''


def norm_company(s):
    return re.sub(r'[^\w]', '', (s or '').casefold())


def save(d):
    vals = clean(d)
    now = datetime.now().isoformat(timespec='seconds')
    if not vals['name'] and not vals['company']:
        raise ValueError('请填写店铺或企业名称')
    if vals['identity'] and not (vals['company'] and vals['evidence_url']):
        raise ValueError('企业主体已核实需要企业名称和背调证据链接')
    if vals['contact_ok'] and not (vals['contact'] and vals['contact_source']):
        raise ValueError('有效商务联系方式需要联系方式及来源')
    if vals['b2b'] and not (vals['notes'] and vals['evidence_url']):
        raise ValueError('B2B能力已核实需要证据链接和背调备注')
    with db() as c:
        if d.get('id'):
            c.execute('UPDATE leads SET updated=?, ' + ','.join(x+'=?' for x in FIELDS) + ' WHERE id=?', [now, *vals.values(), int(d['id'])])
            return 'updated'
        for old in c.execute('SELECT * FROM leads'):
            oldd = dict(old)
            if (canon(vals['url']) and canon(vals['url']) == canon(oldd.get('url')) and vals['country'] == oldd.get('country')) or (norm_company(vals['company']) and norm_company(vals['company']) == norm_company(oldd.get('company')) and vals['country'] == oldd.get('country')):
                return 'duplicate'
        c.execute('INSERT INTO leads(created,updated,' + ','.join(FIELDS) + ') VALUES(' + ','.join('?' for _ in range(len(FIELDS)+2)) + ')', [now, now, *vals.values()])
        return 'added'


def rows():
    with db() as c:
        out = [dict(r) for r in c.execute('SELECT * FROM leads ORDER BY id DESC')]
    for r in out:
        r['score'], r['grade'] = grade(r)
    return out


def serper_search(q, gl, hl, key, num=10):
    body = json.dumps({'q': q, 'gl': gl, 'hl': hl, 'num': num}).encode()
    req = urllib.request.Request('https://google.serper.dev/search', data=body, headers={'X-API-KEY': key, 'Content-Type': 'application/json', 'User-Agent': USER_AGENT}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=30) as f:
            result = json.load(f)
    except urllib.error.HTTPError as e:
        raise ValueError('Serper 搜索服务返回 HTTP ' + str(e.code) + '，请检查 API Key、额度和套餐')
    except urllib.error.URLError:
        raise ValueError('无法连接 Serper 搜索服务，请检查网络')
    if 'organic' not in result and result.get('message'):
        raise ValueError('Serper 未返回正常搜索结果：' + str(result.get('message'))[:200])
    return [{'name': x.get('title',''), 'url': x.get('link',''), 'snippet': x.get('snippet','')} for x in result.get('organic', []) if x.get('link')]


def is_skipped_host(host):
    h = (host or '').lower()
    return any(x in h for x in SKIP_FETCH)


def validate_public_url(url):
    p = urlsplit(url)
    if p.scheme not in ('http','https') or not p.hostname or p.username or p.password:
        raise ValueError('仅允许公开 HTTP/HTTPS 网页')
    host = p.hostname.rstrip('.').lower()
    if host in ('localhost',) or host.endswith('.local'):
        raise ValueError('不允许访问本机或本地域名')
    try:
        infos = socket.getaddrinfo(host, p.port or (443 if p.scheme == 'https' else 80), type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise ValueError('域名无法解析')
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError('出于安全原因，跳过非公网地址')
    return True


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.text=[]; self.title=[]; self.in_title=False; self.skip=0; self.links=[]; self.meta=[]; self.in_a=False
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ('script','style','noscript','svg','nav'):
            self.skip += 1
        if tag == 'title': self.in_title = True
        if tag == 'meta' and (a.get('name','').lower() in ('description','og:description') or a.get('property','').lower() == 'og:description'):
            self.meta.append(a.get('content',''))
        if tag == 'a' and a.get('href'):
            self.links.append({'href': a['href'], 'text': ''}); self.in_a=True
    def handle_endtag(self, tag):
        if tag == 'title': self.in_title = False
        if tag == 'a': self.in_a=False
        if tag in ('script','style','noscript','svg','nav') and self.skip:
            self.skip -= 1
    def handle_data(self, data):
        t = ' '.join(data.split())
        if not t: return
        if self.in_title: self.title.append(t)
        if not self.skip:
            self.text.append(t)
        if self.in_a and self.links and len(self.links[-1]['text']) < 250:
            self.links[-1]['text'] += (' ' if self.links[-1]['text'] else '') + t


def fetch_html(url, timeout=12):
    try:
        validate_public_url(url)
    except ValueError:
        return None
    host = urlsplit(url).hostname or ''
    if is_skipped_host(host):
        return None
    # Respect a site's robots.txt where it can be read.
    robots_url = urljoin(url, '/robots.txt')
    rp = robotparser.RobotFileParser()
    try:
        validate_public_url(robots_url)
        opener = build_opener(SafeRedirect())
        req = urllib.request.Request(robots_url, headers={'User-Agent': USER_AGENT})
        with opener.open(req, timeout=5) as f:
            robots_text = f.read(200000).decode('utf-8', 'ignore')
        rp.parse(robots_text.splitlines())
        if not rp.can_fetch(USER_AGENT, url):
            return None
    except Exception:
        # If robots.txt is unavailable, do not infer permission from it; fetch only the public page itself.
        pass
    opener = build_opener(SafeRedirect())
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT, 'Accept': 'text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1'})
    try:
        with opener.open(req, timeout=timeout) as f:
            final_url = f.geturl()
            validate_public_url(final_url)
            ctype = f.headers.get_content_type()
            if ctype not in ('text/html','application/xhtml+xml','text/plain'):
                return None
            raw = f.read(1_200_000)
            charset = f.headers.get_content_charset() or 'utf-8'
        html = raw.decode(charset, 'replace')
        parser = PageParser(); parser.feed(html)
        text = re.sub(r'\s+', ' ', ' '.join(parser.text)).strip()
        return {'url': final_url, 'title': ' '.join(parser.title)[:300], 'description': ' '.join(parser.meta)[:500], 'text': text[:9000], 'links': parser.links[:300]}
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


def discover_pages(candidate, extra_results, max_pages):
    sources = []
    url_seen = set()
    def add_search(item, kind='search'):
        u = item.get('url','')
        if not canon(u) or canon(u) in url_seen: return
        url_seen.add(canon(u))
        sources.append({'id': 'S'+str(len(sources)+1), 'kind': kind, 'title': item.get('name',''), 'url': u, 'excerpt': item.get('snippet','')[:900], 'fetched': False})
    add_search(candidate, '原始候选')
    for item in extra_results[:18]:
        add_search(item)
    # Fetch only public company/directory pages, never automate marketplace/social pages.
    fetched = []
    source_by_url = {canon(s['url']): s for s in sources}
    for s in list(sources):
        if len(fetched) >= max_pages: break
        host = urlsplit(s['url']).hostname or ''
        if is_skipped_host(host): continue
        page = fetch_html(s['url'])
        if page and page.get('text'):
            s.update({'title': page['title'] or s['title'], 'excerpt': (page['description'] + ' ' + page['text'])[:5000], 'fetched': True, 'links': page['links']})
            fetched.append((s, page))
    # Discover About / Products / Contact / Distributor pages on the same domain only.
    for parent, page in fetched:
        if len(fetched) >= max_pages: break
        base_host = urlsplit(parent['url']).hostname
        candidates = []
        for a in page['links']:
            href = urljoin(page['url'], a['href'])
            p = urlsplit(href)
            if p.hostname != base_host or p.scheme not in ('http','https'):
                continue
            text = (a.get('text','') + ' ' + p.path).lower()
            if any(k in text for k in ('about','company','contact','product','brand','wholesale','distributor','import','export','our-story','who-we-are')):
                if canon(href) not in url_seen:
                    candidates.append((href, a.get('text','')))
        for href, label in candidates[:8]:
            if len(fetched) >= max_pages: break
            url_seen.add(canon(href))
            page2 = fetch_html(href)
            if not page2 or not page2.get('text'): continue
            s = {'id':'S'+str(len(sources)+1), 'kind':'企业网站子页面', 'title':page2['title'] or label, 'url':page2['url'], 'excerpt':(page2['description']+' '+page2['text'])[:5000], 'fetched':True, 'links':page2['links']}
            sources.append(s); fetched.append((s,page2))
    # Public contacts are extracted only from fetched pages and retain source IDs.
    contacts = []
    for s in sources:
        if not s.get('fetched'): continue
        txt = s.get('excerpt','')
        for email in CONTACT_EMAIL_RE.findall(txt):
            if email.lower().endswith(('.png','.jpg','.webp')) or 'example.' in email.lower() or 'noreply' in email.lower(): continue
            contacts.append({'type':'email','value':email,'source_id':s['id'],'url':s['url']})
        for m in CONTACT_PHONE_RE.findall(txt):
            digits = re.sub(r'\D','',m)
            if 8 <= len(digits) <= 15:
                contacts.append({'type':'phone_candidate','value':m.strip(),'source_id':s['id'],'url':s['url']})
        for link in s.get('links', []):
            href = link.get('href','')
            if href.lower().startswith('mailto:'):
                val = href[7:].split('?')[0].strip()
                if CONTACT_EMAIL_RE.fullmatch(val): contacts.append({'type':'email','value':val,'source_id':s['id'],'url':s['url']})
            elif href.lower().startswith('tel:'):
                val = href[4:].strip()
                if 8 <= len(re.sub(r'\D','',val)) <= 15: contacts.append({'type':'phone_candidate','value':val,'source_id':s['id'],'url':s['url']})
            elif 'wa.me/' in href or 'api.whatsapp.com' in href:
                contacts.append({'type':'whatsapp_link','value':href,'source_id':s['id'],'url':s['url']})
    # Deduplicate contact values.
    seen=set(); unique=[]
    for c in contacts:
        k=(c['type'],re.sub(r'\W','',c['value']).lower())
        if k not in seen: seen.add(k); unique.append(c)
    return sources, unique


def build_ai_payload(candidate, sources, contacts):
    source_text = []
    for source in sources:
        source_text.append({'source_id':source['id'],'kind':source['kind'],'title':source['title'],'url':source['url'],'content':source['excerpt'][:5000]})
    instructions = '''你是 BiTop 海外 B2B 客户尽职调查分析助手。只允许依据用户提供的公开网页搜索摘要和网页正文作判断。不得凭常识补全企业法定名称、进口资质、销售规模、员工数量、联系方式、代理关系或认证。每个重要结论必须引用 source_id；没有证据就写 unknown/not_found，并列入 missing_information。搜索摘要只能算弱证据；企业官网、官方登记或明确的企业业务页面优先。电商商品页只能证明存在商品销售线索，不能单独证明进口、批发或分销能力。识别可能的制造商/直接竞争者。不要把未经核实的邮箱/电话当成有效商务联系方式。输出严格 JSON，不要 Markdown。'''
    user = {
        'task': '对候选企业进行公开信息背调，并为 BiTop 汽车养护产品 B2B 开发提供建议。',
        'candidate': candidate,
        'available_contacts_extracted_from_pages': contacts,
        'sources': source_text,
        'required_json_schema': {
            'candidate_name':'string','legal_company_name':'string or unknown','country':'string','website':'string or unknown',
            'candidate_type':'importer|wholesaler|distributor|retailer|manufacturer|brand_owner|service_shop|unknown',
            'business_summary':'string','products':['string'],'brands':['string'],
            'product_match':{'status':'confirmed|indicated|not_found|unknown','matched_products':['string'],'source_ids':['S1'],'reason':'string'},
            'company_identity':{'status':'confirmed|indicated|not_found|unknown','legal_name':'string or unknown','source_ids':['S1'],'reason':'string'},
            'b2b_capabilities':[{'type':'importer|wholesaler|distributor|OEM|private_label|retailer|manufacturer','status':'confirmed|indicated|not_found|unknown','source_ids':['S1'],'reason':'string'}],
            'contacts':[{'type':'email|phone|whatsapp|contact_form','value':'string','source_id':'S1','status':'published_on_source|unverified'}],
            'local_presence':{'status':'confirmed|indicated|not_found|unknown','source_ids':['S1'],'reason':'string'},
            'competitor_risk':{'status':'yes|no|unclear','reason':'string','source_ids':['S1']},
            'risks':['string'],'missing_information':['string'],
            'evidence':[{'claim':'string','source_id':'S1','quote':'short exact quote from source'}],
            'suggested_grade':'A|B|C|待核实','suggested_score':'integer 0-100','grade_reason':'string','overall_confidence':'integer 0-100'
        },
        'grading_rules': 'A only if a fetched public business/official page supports product match, a legally identifiable company (for confirmed identity prefer official registry or legal imprint), importer/wholesaler/distributor or OEM/PL capability, and a published business contact. A search snippet alone is not enough for A. B if product match is at least indicated by a source but A conditions are incomplete. C only if there is clear evidence this is a direct competing manufacturer or irrelevant. Otherwise 待核实. Do not mark A from marketplace sales alone.',
        'important': 'Evidence source IDs must exactly match the supplied sources. If no exact quote exists, do not invent one. Distinguish confirmed from indicated. Contacts extracted from a webpage must retain their source ID and are not necessarily verified as current.'
    }
    return instructions, json.dumps(user, ensure_ascii=False)


def parse_and_sanitize_ai_report(text, sources, contacts):
    text = str(text).strip()
    text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text, flags=re.I)
    start = text.find('{'); end = text.rfind('}')
    try:
        report = json.loads(text[start:end+1])
    except Exception as exc:
        raise ValueError('AI 已返回内容，但未能解析为规范 JSON。请重试或更换模型。') from exc
    if not isinstance(report, dict): raise ValueError('AI 返回的 JSON 结构不是对象，请重试')
    try: report['suggested_score']=max(0,min(100,int(report.get('suggested_score',0) or 0)))
    except (TypeError,ValueError): report['suggested_score']=0
    try: report['overall_confidence']=max(0,min(100,int(report.get('overall_confidence',0) or 0)))
    except (TypeError,ValueError): report['overall_confidence']=0
    if report.get('suggested_grade') not in ('A','B','C','待核实'): report['suggested_grade']='待核实'
    valid_ids={s['id'] for s in sources}; source_by_id={s['id']:s for s in sources}
    strong_ids={s['id'] for s in sources if s.get('fetched')}
    if not isinstance(report.get('evidence'),list): report['evidence']=[]
    for ev in report.get('evidence',[]):
        if not isinstance(ev,dict): continue
        sid=ev.get('source_id')
        if sid not in valid_ids: ev['source_id']='unknown'; ev['quote']=''
        elif ev.get('quote'):
            page_text=(source_by_id[sid].get('excerpt') or '').casefold()
            if str(ev.get('quote')).casefold() not in page_text:
                ev['quote']='（AI 摘录未能与来源正文逐字匹配，请打开原文核对）'
    for key_name in ('product_match','company_identity','local_presence','competitor_risk'):
        obj=report.get(key_name)
        if isinstance(obj,dict):
            ids=obj.get('source_ids',[]); obj['source_ids']=[x for x in ids if x in valid_ids] if isinstance(ids,list) else []
    if not isinstance(report.get('b2b_capabilities'),list): report['b2b_capabilities']=[]
    for item in report.get('b2b_capabilities',[]):
        if not isinstance(item,dict): continue
        ids=item.get('source_ids',[]); item['source_ids']=[x for x in ids if x in valid_ids] if isinstance(ids,list) else []
    if not isinstance(report.get('contacts'),list): report['contacts']=[]
    observed={}
    for c in contacts:
        if c.get('source_id') in strong_ids:
            typ=c.get('type',''); val=c.get('value','')
            if typ=='email': observed[('email',val.casefold())]=c['source_id']
            elif typ=='phone_candidate': observed[('phone',re.sub(r'\D','',val))]=c['source_id']
            elif typ=='whatsapp_link': observed[('whatsapp',val)]=c['source_id']
    for item in report.get('contacts',[]):
        if not isinstance(item,dict): continue
        typ=(item.get('type') or '').lower(); val=str(item.get('value') or '').strip(); sid=item.get('source_id')
        if typ=='email': key=('email',val.casefold())
        elif typ in ('phone','phone_candidate'): key=('phone',re.sub(r'\D','',val))
        elif typ in ('whatsapp','whatsapp_link'): key=('whatsapp',val)
        else: key=(typ,val)
        matched_sid=observed.get(key)
        if matched_sid: item['source_id']=matched_sid; item['status']='published_on_source'
        else:
            item['status']='unverified'
            if sid not in valid_ids: item['source_id']='unknown'
    website=report.get('website')
    if website and website not in ('unknown','not_found',''):
        try:
            wh=(urlsplit(website).hostname or '').lower()
            def same_site(other):
                sh=(urlsplit(source_by_id[other]['url']).hostname or '').lower().removeprefix('www.')
                return wh.removeprefix('www.')==sh or wh.removeprefix('www.').endswith('.'+sh) or sh.endswith('.'+wh.removeprefix('www.'))
            if not wh or not any(same_site(sid) for sid in strong_ids): report['website']='unknown'
        except Exception: report['website']='unknown'
    pm=report.get('product_match') if isinstance(report.get('product_match'),dict) else {}
    ident=report.get('company_identity') if isinstance(report.get('company_identity'),dict) else {}
    caps=report.get('b2b_capabilities') if isinstance(report.get('b2b_capabilities'),list) else []
    b2b_confirmed=any(x.get('status')=='confirmed' and set(x.get('source_ids',[])) & strong_ids and x.get('type') in ('importer','wholesaler','distributor','OEM','private_label') for x in caps if isinstance(x,dict))
    contacts_confirmed=any(x.get('status')=='published_on_source' and x.get('source_id') in strong_ids and x.get('value') for x in report.get('contacts',[]) if isinstance(x,dict))
    match_strong=pm.get('status')=='confirmed' and bool(set(pm.get('source_ids',[])) & strong_ids)
    match_indicated=pm.get('status') in ('confirmed','indicated') and bool(pm.get('source_ids'))
    identity_confirmed=ident.get('status')=='confirmed' and bool(set(ident.get('source_ids',[])) & strong_ids)
    cr=report.get('competitor_risk') if isinstance(report.get('competitor_risk'),dict) else {}
    competitor_yes=cr.get('status')=='yes' and bool(set(cr.get('source_ids',[])) & strong_ids)
    if competitor_yes: report['suggested_grade']='C'
    elif match_strong and identity_confirmed and b2b_confirmed and contacts_confirmed: report['suggested_grade']='A'
    elif match_indicated: report['suggested_grade']='B'
    else: report['suggested_grade']='待核实'
    score=(25 if match_strong else 10 if match_indicated else 0)+(25 if identity_confirmed else 0)+(25 if b2b_confirmed else 0)+(15 if contacts_confirmed else 0)+(10 if isinstance(report.get('local_presence'),dict) and report['local_presence'].get('status')=='confirmed' and set(report['local_presence'].get('source_ids',[])) & strong_ids else 0)
    report['suggested_score']=score
    report['grade_reason']='服务器按证据引用重新计算：产品匹配、企业身份、B2B能力和公开商务联系必须有可对应来源；该等级仍需人工审核。'
    return report


def call_ai_chatgpt(model, candidate, sources, contacts):
    if not model: raise ValueError('请先登录 ChatGPT 并选择一个可用模型')
    instructions, user_text = build_ai_payload(candidate, sources, contacts)
    output = responses_text(model, instructions, user_text)
    return parse_and_sanitize_ai_report(output, sources, contacts)

def research(d):
    if d.get('privacy_consent') is not True:
        raise ValueError('请先阅读并确认数据处理说明')
    name=str(d.get('company') or d.get('name') or '').strip()
    if not name: raise ValueError('请先填写候选企业或店铺名称')
    serper_key=str(d.get('serper_key','')).strip() or os.getenv('SERPER_API_KEY','')
    if not serper_key: raise ValueError('自动企业背调需要 Serper API Key，用于查找企业相关公开网页')
    ai_model=str(d.get('model','') or siwc_status().get('selected_model','')).strip()
    get_access_token()  # fail before spending additional Serper searches if ChatGPT plan authorization is unavailable
    available_models={m['slug'] for m in list_models()}
    if not ai_model or ai_model not in available_models:
        raise ValueError('请先刷新 ChatGPT 可用模型列表并选择有效模型')
    country_code=str(d.get('country','PH')).upper()
    country_name, hl=COUNTRIES.get(country_code, (country_code,'en'))
    product=str(d.get('product','')).strip()
    depth='deep' if d.get('depth')=='deep' else 'quick'
    query_count=5 if depth=='deep' else 2
    queries=[
        f'"{name}" "{country_name}" official website company',
        f'"{name}" "{country_name}" importer distributor wholesaler contact',
        f'"{name}" "{country_name}" products brands automotive',
        f'"{name}" "{product}" "{country_name}"',
        f'"{name}" "{country_name}" contact email phone whatsapp',
        f'"{name}" "{country_name}" company registration legal entity registry'
    ][:query_count]
    candidate={'name':str(d.get('name') or name),'company':str(d.get('company') or ''),'country':country_code,'country_name':country_name,'product':product,'channel':str(d.get('channel','')),'url':str(d.get('url','')),'snippet':str(d.get('snippet',''))}
    extra=[]; seen=set()
    for q in queries:
        for item in serper_search(q,country_code.lower(),hl,serper_key,10):
            u=canon(item.get('url',''))
            if u and u not in seen:
                seen.add(u); extra.append(item)
    max_pages=4 if depth=='quick' else 7
    sources, contacts=discover_pages(candidate,extra,max_pages)
    report=call_ai_chatgpt(ai_model,candidate,sources,contacts)
    return {'report':report,'sources':sources,'queries':queries,'contacts_found':contacts,'meta':{'provider':'ChatGPT plan usage','model':ai_model,'search_queries':len(queries),'search_results':len(extra),'pages_read':sum(1 for s in sources if s.get('fetched')),'depth':depth,'research_at':datetime.now().isoformat(timespec='seconds')}}


class Handler(BaseHTTPRequestHandler):
    def send(self,data,typ='application/json; charset=utf-8',code=200,filename=None):
        if not isinstance(data,bytes): data=json.dumps(data,ensure_ascii=False).encode()
        self.send_response(code); self.send_header('Content-Type',typ); self.send_header('Content-Length',str(len(data))); self.send_header('Cache-Control','no-store'); self.send_header('X-Content-Type-Options','nosniff'); self.send_header('Referrer-Policy','no-referrer')
        if filename: self.send_header('Content-Disposition','attachment; filename='+filename)
        self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        parsed=urlsplit(self.path); p=parsed.path
        # OAuth redirects are top-level cross-site navigations; state + PKCE protect this route.
        try:
            if p == '/auth/callback':
                validate_local_host(self.headers)
            else:
                validate_local_request(self.headers)
        except ValueError as e:
            self.send({'error': str(e)}, code=403)
            return
        if p=='/auth/callback':
            try:
                result=complete_callback(urllib.parse.parse_qs(parsed.query))
                message='ChatGPT 授权完成。' if result.get('plan_usage_enabled') else '账户已登录，但未授予 ChatGPT 计划用量权限。请返回系统检查授权。'
                color='#087d76' if result.get('plan_usage_enabled') else '#9a6700'
                html=f'''<!doctype html><html lang=zh-CN><meta charset=utf-8><title>BiTop ChatGPT 授权</title><body style="font-family:Arial,sans-serif;padding:48px;color:#18333d"><h2 style="color:{color}">{message}</h2><p>{html_escape(str(result.get('email','')))}</p><p>此页面将在 3 秒后返回 BiTop Global Lead Finder。</p><a href="/">返回系统</a><script>setTimeout(()=>location.replace('/'),3000)</script></body></html>'''
                self.send(html,'text/html; charset=utf-8')
            except Exception as e:
                msg=html_escape(str(e))
                html=f'''<!doctype html><html lang=zh-CN><meta charset=utf-8><title>授权未完成</title><body style="font-family:Arial,sans-serif;padding:48px"><h2>ChatGPT 授权未完成</h2><p>{msg}</p><a href="/">返回系统并重新尝试</a></body></html>'''
                self.send(html,'text/html; charset=utf-8',code=400)
        elif p=='/': self.send((ROOT/'index.html').read_bytes(),'text/html; charset=utf-8')
        elif p=='/privacy': self.send((ROOT/'privacy.html').read_bytes(),'text/html; charset=utf-8')
        elif p=='/api/leads': self.send(rows())
        elif p=='/api/auth/status':
            try: self.send(siwc_status())
            except Exception as e: self.send({'error':str(e)[:300]},code=400)
        elif p=='/api/auth/models':
            try:
                models=list_models(); store_model=siwc_status().get('selected_model','')
                self.send({'models':models,'selected_model':store_model})
            except Exception as e: self.send({'error':str(e)[:300]},code=400)
        elif p=='/api/settings': self.send(load_settings())
        elif p=='/api/export':
            buf=io.StringIO(); w=csv.DictWriter(buf,fieldnames=['grade','score','id',*FIELDS,'created','updated']); w.writeheader()
            for row in rows():
                for k,v in row.items():
                    if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@','\t','\r')): row[k]="'"+v
                w.writerow(row)
            self.send(buf.getvalue().encode('utf-8-sig'),'text/csv; charset=utf-8',filename='BiTop_leads.csv')
        else: self.send({'error':'Not found'},code=404)
    def do_POST(self):
        try:
            validate_local_request(self.headers, require_origin=True)
            n=int(self.headers.get('Content-Length',0))
            if n>MAX_BODY: raise ValueError('请求数据过大')
            d=json.loads(self.rfile.read(n)); p=urlsplit(self.path).path
            if p=='/api/auth/start':
                settings=save_settings({'public_repo_url':d.get('public_repo_url','')})
                redirect_uri='http://127.0.0.1:8765/auth/callback'
                auth_url=start_signin(settings.get('public_repo_url',''),redirect_uri)
                opened=webbrowser.open(auth_url, new=1)
                self.send({'ok':True,'opened':bool(opened),'message':'已在默认浏览器打开 OpenAI 授权页面。','authorization_url':auth_url})
            elif p=='/api/auth/disconnect': self.send(siwc_disconnect())
            elif p=='/api/auth/model': self.send({'selected_model':set_selected_model(d.get('model',''))})
            elif p=='/api/settings': self.send(save_settings(d))
            elif p=='/api/save': self.send({'result':save(d)})
            elif p=='/api/batch':
                counts={'added':0,'duplicate':0,'updated':0,'errors':[]}
                for i,item in enumerate(d.get('items',[])[:500]):
                    try: counts[save({k:v for k,v in item.items() if k!='id'})]+=1
                    except ValueError as e: counts['errors'].append(f'{i+1}: {e}')
                self.send(counts)
            elif p=='/api/search':
                if d.get('privacy_consent') is not True: raise ValueError('请先阅读并确认数据处理说明')
                self.send(serper_search(str(d.get('q','')),str(d.get('gl','ph')),str(d.get('hl','en')),str(d.get('key','')) or os.getenv('SERPER_API_KEY',''),10))
            elif p=='/api/research': self.send(research(d))
            elif p=='/api/delete':
                with db() as c: c.execute('DELETE FROM leads WHERE id=?',(int(d['id']),))
                self.send({'ok':True})
            else: self.send({'error':'Not found'},code=404)
        except (ValueError,KeyError,TypeError) as e: self.send({'error':str(e)},code=400)
        except Exception as e:
            self.send({'error':'操作失败：'+str(e)[:300]},code=500)
    def log_message(self, fmt, *args):
        # Never log OAuth callback query strings (authorization code/state).
        if getattr(self, 'path', '').startswith('/auth/callback'):
            super().log_message('OAuth callback received (query redacted)')
            return
        # Do not log request bodies or API keys.
        super().log_message(fmt,*args)

if __name__=='__main__':
    init(); server=ThreadingHTTPServer(('127.0.0.1',8765),Handler); url='http://127.0.0.1:8765'
    print('BiTop Global Lead Finder V1.2.1:',url,'\nKeep this window open. Ctrl+C to stop.',flush=True)
    if '--no-browser' not in sys.argv: threading.Timer(.5,lambda:webbrowser.open(url)).start()
    try: server.serve_forever()
    except KeyboardInterrupt: server.server_close()
