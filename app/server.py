import argparse
import json
import sqlite3
import uuid
import traceback
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .data import CONFIG, DB, ROOT, connect
from .evidence import cutoff_value, get_prepared, normalize_drug, now, prepare_patient, review_drug, version, visible
from . import agent

WORKER = ThreadPoolExecutor(max_workers=1, thread_name_prefix='prepare')
STATIC = ROOT / 'web'


def patients(conn, cutoff=''):
    result=[]
    for r in conn.execute('''SELECT p.subject_id,p.gender,p.anchor_age,p.anchor_year,
                    COUNT(a.hadm_id) admission_count, MAX(a.dischtime) last_discharge
                    FROM hosp_patients p LEFT JOIN hosp_admissions a ON p.subject_id=a.subject_id
                    GROUP BY p.subject_id ORDER BY p.subject_id'''):
        row=dict(r)
        row['prepared']=conn.execute('SELECT prepared_at FROM prepared WHERE subject_id=? AND cutoff=? AND version=?',
                                  (row['subject_id'],cutoff,version(conn))).fetchone()
        row['prepared_at']=row['prepared'][0] if row['prepared'] else None
        row['prepared']=bool(row['prepared'])
        if cutoff:
            row['admission_count']=conn.execute('SELECT COUNT(*) FROM hosp_admissions WHERE subject_id=? AND admittime<=?',(row['subject_id'],cutoff)).fetchone()[0]
            row['last_discharge']=None
        result.append(row)
    return result


def catalog(conn, sid=None, cutoff=''):
    if sid:
        bundle=get_prepared(conn,sid,cutoff)
        if not bundle:
            return []
        names=[r['drug'] for r in bundle['exposures'] if r['drug']]
    else:
        names=[r[0] for r in conn.execute('''SELECT drug FROM hosp_prescriptions UNION SELECT medication FROM hosp_emar
             UNION SELECT d.label FROM icu_inputevents x JOIN icu_d_items d ON x.itemid=d.itemid
             WHERE d.category IN ('Medications','Antibiotics')''') if r[0]]
    unique={}
    for name in names:
        unique.setdefault(normalize_drug(name),name)
    return sorted(unique.values(),key=str.casefold)


def run_job(jid, ids, cutoff):
    with connect() as conn:
        conn.execute("UPDATE jobs SET state='running' WHERE id=?",(jid,))
    for sid in ids:
        with connect() as conn:
            conn.execute("UPDATE job_items SET state='running' WHERE job_id=? AND subject_id=?",(jid,sid))
        try:
            prepare_patient(sid,cutoff)
            state,error='complete',None
        except Exception as exc:
            state,error='failed',str(exc)
        with connect() as conn:
            conn.execute('UPDATE job_items SET state=?,error=? WHERE job_id=? AND subject_id=?',(state,error,jid,sid))
            conn.execute('UPDATE jobs SET done=done+1 WHERE id=?',(jid,))
    with connect() as conn:
        failures=conn.execute("SELECT COUNT(*) FROM job_items WHERE job_id=? AND state='failed'",(jid,)).fetchone()[0]
        conn.execute('UPDATE jobs SET state=? WHERE id=?',('partial' if failures else 'complete',jid))


class Handler(BaseHTTPRequestHandler):
    def reply(self, payload, status=200):
        body=json.dumps(payload,ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            self.route_get()
        except ValueError as exc:
            self.reply({'error':str(exc)},400)
        except Exception:
            traceback.print_exc()
            self.reply({'error':'내부 오류입니다. 서버 로그를 확인하세요.'},500)

    def route_get(self):
        url=urlparse(self.path)
        if not url.path.startswith('/api/'):
            files={'/':'index.html','/app.js':'app.js','/style.css':'style.css'}
            name=files.get(url.path)
            if not name:
                self.send_error(404); return
            content=(STATIC/name).read_bytes()
            self.send_response(200)
            self.send_header('Content-Type',{'html':'text/html; charset=utf-8','js':'text/javascript; charset=utf-8','css':'text/css; charset=utf-8'}[name.split('.')[-1]])
            self.send_header('Content-Length',str(len(content)))
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers(); self.wfile.write(content); return
        params={k:v[0] for k,v in parse_qs(url.query).items()}
        cutoff=cutoff_value(params.get('cutoff',''))
        with connect() as conn:
            if url.path=='/api/agent/status':
                self.reply(agent.status())
            elif url.path=='/api/agent/history':
                values=[dict(r) for r in conn.execute('''SELECT id,state,created_at FROM agent_runs
                    WHERE subject_id=? AND drug=? AND cutoff=? AND evidence_version=? ORDER BY created_at DESC,id DESC LIMIT 5''',
                    (params.get('subject_id',''),params.get('drug',''),cutoff,version(conn)))]
                self.reply({'runs':values})
            elif url.path.startswith('/api/agent/runs/'):
                rid=url.path.rsplit('/',1)[-1]
                row=conn.execute('SELECT * FROM agent_runs WHERE id=?',(rid,)).fetchone()
                if not row:
                    self.reply({'error':'AI 작업을 찾을 수 없습니다.'},404); return
                value=dict(row)
                for key in ('result','trace'):
                    value[key]=json.loads(value[key]) if value[key] else None
                self.reply(value)
            elif url.path=='/api/meta':
                meta={r['key']:json.loads(r['value']) for r in conn.execute('SELECT * FROM metadata')}
                meta.pop('fingerprint',None)
                meta.update({'patient_count':conn.execute('SELECT COUNT(*) FROM hosp_patients').fetchone()[0],
                    'pipeline_version':version(conn).split(':')[0], 'notes_available':False})
                self.reply(meta)
            elif url.path=='/api/patients':
                self.reply(patients(conn,cutoff))
            elif url.path=='/api/drugs':
                q=params.get('q','').casefold()
                values=[n for n in catalog(conn,params.get('subject_id'),cutoff) if q in n.casefold()]
                self.reply({'items':values[:200],'total':len(values)})
            elif url.path=='/api/patient':
                sid=params.get('subject_id','')
                bundle=get_prepared(conn,sid,cutoff)
                if not bundle:
                    self.reply({'error':'선택한 기준 시점으로 사전 처리를 먼저 진행하세요.'},409); return
                self.reply({k:v for k,v in bundle.items() if k not in {'exposures','labs','vitals'}} | {
                    'counts':{k:len(bundle[k]) for k in ['exposures','labs','vitals','diagnoses']},
                    'drugs':catalog(conn,sid,cutoff),
                    'labs':sorted(bundle['labs'],key=lambda r:r['time'],reverse=True)[:100],
                    'labs_total':len(bundle['labs'])})
            elif url.path=='/api/review':
                sid=params.get('subject_id','')
                bundle=get_prepared(conn,sid,cutoff)
                if not bundle:
                    self.reply({'error':'사전 처리 결과가 없습니다.'},409); return
                drug=params.get('drug','').strip()
                if not drug or len(drug)>200:
                    raise ValueError('약물명을 입력하세요. 최대 200자입니다.')
                result=review_drug(bundle,drug)
                result['vital_context_total']=len(result['vital_context'])
                result['vital_context']=result['vital_context'][:100]
                offset=max(0,int(params.get('offset',0)))
                result['exposures']=result['exposures'][offset:offset+100]
                result['offset']=offset
                result['reviews']=[dict(r) for r in conn.execute('''SELECT * FROM reviews WHERE subject_id=?
                    AND drug=? AND cutoff=? AND evidence_version=? ORDER BY id DESC''',(sid,normalize_drug(drug),cutoff,version(conn)))]
                self.reply(result)
            elif url.path.startswith('/api/jobs/'):
                jid=url.path.rsplit('/',1)[-1]
                row=conn.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()
                if not row:
                    self.reply({'error':'작업을 찾을 수 없습니다.'},404); return
                self.reply(dict(row)|{'items':[dict(r) for r in conn.execute('SELECT * FROM job_items WHERE job_id=?',(jid,))]})
            elif url.path=='/api/source':
                table=params.get('table',''); sid=params.get('subject_id','')
                allowed={'hosp_emar','hosp_emar_detail','hosp_prescriptions','hosp_diagnoses_icd','hosp_labevents','icu_chartevents','icu_inputevents'}
                if table not in allowed:
                    raise ValueError('허용되지 않은 원본 테이블입니다.')
                row=conn.execute(f'SELECT * FROM {table} WHERE source_row=? AND subject_id=?',(int(params.get('row','0')),sid)).fetchone()
                if not row:
                    self.reply({'error':'원본 행을 찾을 수 없습니다.'},404); return
                raw=dict(row)
                if cutoff:
                    permitted=False
                    if table in {'hosp_emar','hosp_labevents','icu_chartevents'}:
                        permitted=visible(raw,cutoff)
                    elif table=='hosp_emar_detail':
                        parent=conn.execute('SELECT * FROM hosp_emar WHERE emar_id=? AND subject_id=?',(raw['emar_id'],sid)).fetchone()
                        permitted=bool(parent and visible(dict(parent),cutoff))
                    elif table=='icu_inputevents':
                        permitted=False
                    elif table=='hosp_diagnoses_icd':
                        parent=conn.execute('SELECT dischtime FROM hosp_admissions WHERE hadm_id=? AND subject_id=?',(raw['hadm_id'],sid)).fetchone()
                        permitted=bool(parent and parent[0] and parent[0]<=cutoff)
                    if not permitted:
                        self.reply({'error':'기준 시점에 이용 가능하지 않은 자료입니다.'},403); return
                self.reply({'table':table,'row':raw,'cutoff':cutoff})
            else:
                self.reply({'error':'경로를 찾을 수 없습니다.'},404)

    def do_POST(self):
        try:
            origin=self.headers.get('Origin')
            expected=f'http://{self.headers.get("Host")}'
            if (origin and origin!=expected) or self.headers.get('X-Local-Request')!='1':
                self.reply({'error':'로컬 UI에서 요청하세요.'},403); return
            if not self.headers.get('Content-Type','').startswith('application/json'):
                raise ValueError('JSON 요청이 필요합니다.')
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=20000:
                raise ValueError('요청 크기가 잘못되었습니다.')
            data=json.loads(self.rfile.read(size))
            cutoff=cutoff_value(data.get('cutoff',''))
            with connect() as conn:
                if self.path=='/api/agent/runs':
                    sid=str(data.get('subject_id','')); drug=data.get('drug','')
                    if not isinstance(drug,str) or not drug.strip() or len(drug)>200:
                        raise ValueError('약물명을 입력하세요.')
                    drug=drug.strip()
                    bundle=get_prepared(conn,sid,cutoff)
                    if not bundle:raise ValueError('선택한 범위의 사전 정리가 필요합니다.')
                    if data.get('version')!=bundle['version']:raise ValueError('다시 근거를 조회하세요.')
                    if not agent.status()['available']:
                        self.reply({'error':'로컬 모델 서버 또는 모델을 준비하세요.'},503); return
                    if not agent.LOCK.acquire(blocking=False):
                        self.reply({'error':'다른 AI 검토가 실행 중입니다. 완료 후 다시 실행하세요.'},409); return
                    rid=uuid.uuid4().hex
                    try:
                        conn.execute('INSERT INTO agent_runs(id,subject_id,drug,cutoff,evidence_version,model,state,created_at) VALUES (?,?,?,?,?,?,?,?)',
                            (rid,sid,drug,cutoff,bundle['version'],agent.SETTINGS['model'],'queued',now()))
                        conn.commit()
                        agent.WORKER.submit(agent.run_job,rid,bundle,drug)
                    except Exception:
                        agent.LOCK.release();raise
                    self.reply({'run_id':rid},202)
                elif self.path=='/api/prepare':
                    raw_ids=data.get('subject_ids',[])
                    if not isinstance(raw_ids,list) or not 1<=len(raw_ids)<=100:
                        raise ValueError('환자 1~100명을 선택하세요.')
                    ids=list(dict.fromkeys(str(sid) for sid in raw_ids))
                    valid={r[0] for r in conn.execute('SELECT subject_id FROM hosp_patients')}
                    if not set(ids)<=valid:
                        raise ValueError('존재하지 않는 환자가 포함되어 있습니다.')
                    jid=uuid.uuid4().hex
                    conn.execute('INSERT INTO jobs(id,state,total,created_at) VALUES (?,?,?,?)',(jid,'queued',len(ids),now()))
                    conn.executemany('INSERT INTO job_items VALUES (?,?,?,NULL)',[(jid,sid,'queued') for sid in ids])
                    conn.commit()
                    WORKER.submit(run_job,jid,ids,cutoff)
                    self.reply({'job_id':jid},202)
                elif self.path=='/api/decisions':
                    sid=str(data.get('subject_id','')); drug=data.get('drug','').strip()
                    decision=data.get('decision'); comment=data.get('comment','').strip()
                    if decision not in {'reviewed','defer','reject'} or not drug or len(drug)>200 or len(comment)>2000:
                        raise ValueError('검토 입력이 올바르지 않습니다.')
                    if not get_prepared(conn,sid,cutoff):
                        raise ValueError('사전 처리된 환자가 아닙니다.')
                    if data.get('version')!=version(conn):
                        raise ValueError('분석 버전이 변경되었습니다. 다시 조회하세요.')
                    cur=conn.execute('INSERT INTO reviews(subject_id,drug,cutoff,evidence_version,decision,comment,created_at) VALUES (?,?,?,?,?,?,?)',
                        (sid,normalize_drug(drug),cutoff,version(conn),decision,comment,now()))
                    conn.commit()
                    self.reply({'id':cur.lastrowid,'message':'로컬 검토 기록을 저장했습니다. 알레르기 등록은 수행하지 않습니다.'},201)
                else:
                    self.reply({'error':'경로를 찾을 수 없습니다.'},404)
        except (ValueError,TypeError,json.JSONDecodeError) as exc:
            self.reply({'error':str(exc)},400)
        except Exception:
            self.reply({'error':'내부 오류입니다.'},500)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=CONFIG['port'])
    args=parser.parse_args()
    if not DB.exists():
        raise SystemExit('먼저 python -m app.data 를 실행하세요.')
    with connect() as conn:
        agent.init_storage(conn)
        conn.execute("UPDATE agent_runs SET state='failed',error='서버 재시작으로 중단됨',finished_at=? WHERE state IN ('queued','running')",(now(),))
        conn.execute("UPDATE jobs SET state='failed',error='서버 재시작으로 중단됨' WHERE state IN ('queued','running')")
        conn.execute("UPDATE job_items SET state='failed',error='서버 재시작으로 중단됨' WHERE state IN ('queued','running')")
    server=ThreadingHTTPServer((CONFIG['host'],args.port),Handler)
    print(f'Local app: http://{CONFIG["host"]}:{args.port}',flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        WORKER.shutdown(wait=True)
        agent.WORKER.shutdown(wait=True)


if __name__=='__main__':
    main()
