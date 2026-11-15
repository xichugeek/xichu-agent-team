"""Episode-only teaching fixture behind the unchanged Studio frontend. No live DB/API."""
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import hashlib, json, shutil

ROOT=Path(__file__).resolve().parent
WEB=ROOT/'runtime/demo-dashboard'
WEB.mkdir(parents=True,exist_ok=True)
source=ROOT/'control_center/static'
static=source
hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in static.iterdir() if p.is_file()}
(WEB/'frontend_provenance.json').write_text(json.dumps({'source':'control_center/static','sha256':hashes,'data':'isolated teaching fixture; no production reads/writes'},indent=2),encoding='utf-8')
NOW='2026-11-15T04:30:00Z'
ep={'ref':'026_teaching_example','id':'026','title':'教学示例：从一张工单找到交付',
    'test_only':False,'overall_status':'review_required','stage':'剪辑','current_role':'视频制作',
    'progress':60,'next_action':'继续检查画面、字幕和声音，再由本人审片。','status_modified_at':NOW,
    'validate':{'status':'PENDING','issues':[]},'blockers':[],'obsolete':[],'review_required':[],
    'stats':{'shots':11,'width':1920,'height':1080,'fps':60,'duration':None,'captions':None},
    'candidate':{'master_exists':False,'captions_exist':False,'cover_exists':False,'native_ready':False,'native_directory_exists':False,'video_ready':False},
    'pipeline':[],'progress_breakdown':[],'team':[],
    'gates':{k:{'status':'pending','formal':k!='FACT_REVIEW'} for k in ['FACT_REVIEW','SCRIPT_APPROVAL','FINAL_REVIEW','PUBLISH_APPROVAL']},
    'deliveries':[],'warnings':['教学数据，不表示本期实时制作状态。'],'fingerprint':'teaching-026','directory':'教学工作区'}
snapshot={'studio':{'lan_read_only':False,'active_episode':ep['ref'],'scanned_at':NOW},'episodes':[ep], 'episode':ep,'health':[]}
accounts=[{'id':'codex_'+x.lower(),'label':'Codex 账号 '+x,'status':'ok','email':'教学账号 '+x,
    'primary_roles':[role],'plan':'plus','checked_at':NOW,'windows':[{'label':'5小时窗口','bucket':'codex','remaining_percent':h,'resets_at':'2026-11-15T09:00:00Z'}, {'label':'每周窗口','bucket':'codex','remaining_percent':w,'resets_at':'2026-11-20T04:30:00Z'}],
    'reset_credits':{'available_count':0,'credits':[]}} for x,role,h,w in [('A','video',68,46),('B','site',84,72),('C','pm',52,38)]]
def ticket(tid,role,title):
    return {'id':tid,'title':title,'request':title+'。仅做本地检查，交付可审阅文件。',
        'state':'draft_ready','state_label':'草稿就绪','role_label':{'video':'视频制作','site':'官网运维'}[role],
        'stage_label':'本轮交付完成','executor':'codex_a' if role=='video' else 'codex_b',
        'updated_at':NOW,'episodes':[{'ref':ep['ref'],'id':'026','title':ep['title'],'source':'教学示例'}],
        'completed_stages':1,'attempts':1,'round':1,'simulated':True,
        'stages':[{'label':'视频制作' if role=='video' else '官网检查','status':'ok','executor':'codex_a' if role=='video' else 'codex_b',
            'round':1,'attempt':1,'simulated':True,'started_at':'2026-11-15T04:20:00Z','finished_at':NOW,
            'summary':'教学示例：本次请求已交付，整期状态请查看视频制作页。',
            'progress':['已读取需求和允许范围','已整理本地结果','已交付待审阅说明'],
            'artifacts':[{'name':'output/handoff.md','bytes':2380}]}]}
tickets=[ticket('D-0026','video','整理第026期的剪辑准备说明'),ticket('D-0027','site','检查官网资料链接，输出本地报告')]
agent={'status':'ok','scheduler_reachable':True,'video_mode':'studio','message':'教学示例 · 与真实工单库隔离',
    'counts':{'draft_ready':2},'total':2,'site_total':1,'scanned_at':NOW,'accounts':accounts,'tickets':tickets,'ticket':tickets[0]}
site={'status':'ok','scheduler_reachable':True,'message':'教学示例 · 本地检查记录',
    'project':{'exists':True,'wordpress':True,'path':'官网教学工作区'},'account_pool':['codex_b','codex_c','codex_a'],
    'counts':{'draft_ready':1},'total':1,'scanned_at':NOW,'accounts':accounts,'tickets':[tickets[1]],'ticket':tickets[1]}
fixture={'snapshot':snapshot,'agent':agent,'site':site}
(WEB/'fixture.json').write_text(json.dumps(fixture,ensure_ascii=False,indent=2),encoding='utf-8')
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url=urlparse(self.path)
        if url.path.startswith('/api/'):
            key=url.path[5:]
            if key not in fixture: self.send_error(404); return
            data=fixture[key].copy()
            if key=='agent':
                wanted=parse_qs(url.query).get('ticket',['D-0026'])[0]
                data['ticket']=next((t for t in tickets if t['id']==wanted),tickets[0])
            body=json.dumps(data,ensure_ascii=False).encode()
            mime='application/json; charset=utf-8'
        elif url.path.startswith('/static/'):
            path=static/url.path.split('/')[-1]
            if not path.is_file(): self.send_error(404); return
            body=path.read_bytes(); mime='text/css' if path.suffix=='.css' else 'text/javascript'
        else:
            body=(static/'index.html').read_text(encoding='utf-8').replace('<body>', '<body><div style="position:fixed;right:28px;top:12px;z-index:9999;background:#096777;color:white;padding:8px 18px;border-radius:5px;font:600 18px Microsoft YaHei">教学示例 · 独立数据</div>').encode()
            mime='text/html; charset=utf-8'
        self.send_response(200); self.send_header('Content-Type',mime); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_POST(self): self.send_error(403,'Teaching fixture is read-only')
    def log_message(self,*args): pass
if __name__ == '__main__':
    import argparse, webbrowser
    parser=argparse.ArgumentParser(description='Read-only teaching dashboard; no bot tokens, accounts or production database')
    parser.add_argument('--port',type=int,default=18766)
    parser.add_argument('--open',action='store_true')
    args=parser.parse_args()
    if not 1024 <= args.port <= 65535: parser.error('Port must be 1024..65535')
    server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    url=f'http://127.0.0.1:{server.server_port}/'
    print('Teaching example only; no live tasks or quota calls. '+url,flush=True)
    if args.open:webbrowser.open(url)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
