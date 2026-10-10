"""Deterministic evidence preparation; never diagnoses allergy or predicts risk."""
import json
import re
from datetime import datetime, timedelta, timezone
from .data import connect

PIPELINE_VERSION = 'evidence-v1'
ADMINISTERED = {'administered', 'delayed administered', 'partial administered',
                'administered in other location', 'administered bolus from iv drip',
                'started', 'restarted', 'started in other location', 'applied',
                'applied in other location', 'removed existing / applied new'}
NOT_GIVEN = {'not given', 'hold dose', 'not started', 'not applied', 'not given per sliding scale'}
REACTION = re.compile(r'anaphyl|stevens.?johnson|toxic epidermal necrolysis|drug.*(?:allerg|dermatitis)|allerg.*drug|drug reaction with eosinophilia', re.I)
CONFOUNDER = re.compile(r'sepsis|septic shock', re.I)
LABS = re.compile(r'eosinophil|alanine aminotransferase|aspartate aminotransferase|bilirubin|^creatinine$|tryptase', re.I)
VITAL_LABELS = {'Arterial Blood Pressure systolic', 'Non Invasive Blood Pressure systolic',
                'Arterial Blood Pressure mean', 'Non Invasive Blood Pressure mean',
                'Temperature Celsius', 'Temperature Fahrenheit'}


def now():
    return datetime.now(timezone.utc).isoformat()


def normalize_drug(text):
    # Conservative name cleanup. No salt/brand/class equivalence is inferred.
    return re.sub(r'\s+', ' ', text.strip()).casefold()


def cutoff_value(text):
    if not text:
        return ''
    text = text.replace('T', ' ')
    try:
        value = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError('기준 시점은 YYYY-MM-DD HH:MM:SS 형식이어야 합니다.') from exc
    if value.tzinfo:
        raise ValueError('MIMIC의 이동된 날짜는 시간대 없는 값으로 입력하세요.')
    return value.isoformat(sep=' ', timespec='seconds')


def visible(row, cutoff, event_key='charttime', availability_key='storetime'):
    if not cutoff:
        return True
    event = row.get(event_key, '')
    available = row.get(availability_key, '')
    # Missing availability is excluded, rather than assumed available historically.
    return bool(event and available and event <= cutoff and available <= cutoff)


def exposure_status(event):
    text = (event or '').strip().casefold()
    if text in ADMINISTERED:
        return 'administration_recorded'
    if text in NOT_GIVEN or text.startswith('not '):
        return 'not_given'
    return 'uncertain'


def version(conn):
    row = conn.execute("SELECT value FROM metadata WHERE key='fingerprint'").fetchone()
    return PIPELINE_VERSION + ':' + (json.loads(row[0]) if row else 'test')


def source(row, table):
    return {'table': table, 'row': row['source_row']}


def rows(conn, table, sid):
    return [dict(r) for r in conn.execute(f'SELECT * FROM {table} WHERE subject_id=?', (sid,))]


def prepare_patient(sid, cutoff='', conn=None):
    cutoff = cutoff_value(cutoff)
    owned = conn is None
    conn = conn or connect()
    try:
        patient = conn.execute('SELECT * FROM hosp_patients WHERE subject_id=?', (sid,)).fetchone()
        if not patient:
            raise ValueError('존재하지 않는 환자입니다.')
        rev = version(conn)
        cached = conn.execute('SELECT bundle FROM prepared WHERE subject_id=? AND cutoff=? AND version=?', (sid, cutoff, rev)).fetchone()
        if cached:
            return json.loads(cached[0])
        all_admissions = rows(conn, 'hosp_admissions', sid)
        admission_map = {r['hadm_id']: r for r in all_admissions}
        admissions = [r for r in all_admissions if not cutoff or r['admittime'] <= cutoff]
        for admission in admissions:
            # Do not expose later discharge/outcome information in as-of mode.
            if cutoff and (not admission['dischtime'] or admission['dischtime'] > cutoff):
                for key in ['dischtime', 'deathtime', 'discharge_location', 'hospital_expire_flag']:
                    admission[key] = ''
        exposures = []
        excluded = {'emar': 0, 'prescriptions': 0, 'inputevents': 0, 'diagnoses': 0, 'labs': 0, 'vitals': 0}
        details = {}
        for row in rows(conn, 'hosp_emar_detail', sid):
            details.setdefault(row['emar_id'], []).append(row)
        for row in rows(conn, 'hosp_emar', sid):
            if not visible(row, cutoff):
                excluded['emar'] += 1
                continue
            detail_rows = details.get(row['emar_id'], [])
            # Details are attached to their parent eMAR, without fan-out or dose sums.
            exposures.append({'drug': row['medication'], 'key': normalize_drug(row['medication']),
                'time': row['charttime'], 'available_at': row['storetime'], 'hadm_id': row['hadm_id'],
                'kind': 'emar', 'status': exposure_status(row['event_txt']), 'event': row['event_txt'] or '상태 미기재',
                'source': source(row, 'hosp_emar'),
                'details': [{'dose': r['dose_given'], 'unit': r['dose_given_unit'], 'route': r['route'],
                             'source': source(r, 'hosp_emar_detail')} for r in detail_rows]})
        for row in rows(conn, 'hosp_prescriptions', sid):
            if cutoff:
                # prescriptions has no storage timestamp. Strict historical mode excludes it.
                excluded['prescriptions'] += 1
                continue
            exposures.append({'drug': row['drug'], 'key': normalize_drug(row['drug']),
                'time': row['starttime'], 'endtime': row['stoptime'], 'available_at': None,
                'hadm_id': row['hadm_id'], 'kind': 'prescription', 'status': 'ordered_only',
                'event': '처방 · 투약 확인 아님', 'source': source(row, 'hosp_prescriptions'),
                'details': [{'dose':row['dose_val_rx'], 'unit':row['dose_unit_rx'], 'route':row['route']}]})
        item_map = {r['itemid']: dict(r) for r in conn.execute('SELECT * FROM icu_d_items')}
        for row in rows(conn, 'icu_inputevents', sid):
            item = item_map.get(row['itemid'], {})
            if item.get('category') not in {'Medications', 'Antibiotics'}:
                continue
            # inputevents contains final interval status/amounts rather than a versioned
            # point-in-time audit trail. Strict as-of mode excludes the entire interval.
            if cutoff:
                excluded['inputevents'] += 1
                continue
            rewritten = row['statusdescription'].casefold() == 'rewritten'
            # Final total and endtime may be future information: omit in historical mode.
            exposures.append({'drug':item['label'], 'key':normalize_drug(item['label']),
                'time':row['starttime'], 'endtime': row['endtime'] if not cutoff else '',
                'available_at':row['storetime'], 'hadm_id':row['hadm_id'], 'kind':'icu_input',
                'status':'not_given' if rewritten else 'infusion_recorded',
                'event':row['statusdescription'], 'source':source(row,'icu_inputevents'),
                'details':[{'dose':row['amount'] if not cutoff else '', 'unit':row['amountuom'], 'route':''}]})
        diagnoses = []
        sql = '''SELECT x.*, d.long_title FROM hosp_diagnoses_icd x
                 LEFT JOIN hosp_d_icd_diagnoses d ON x.icd_code=d.icd_code AND x.icd_version=d.icd_version
                 WHERE x.subject_id=?'''
        for record in conn.execute(sql, (sid,)):
            row = dict(record)
            available = admission_map.get(row['hadm_id'], {}).get('dischtime','')
            if cutoff and (not available or available > cutoff):
                excluded['diagnoses'] += 1
                continue
            title = row.get('long_title') or row['icd_code']
            diagnoses.append({'title':title, 'code':row['icd_code'], 'icd_version':row['icd_version'],
                'hadm_id':row['hadm_id'], 'available_at':available, 'onset':None,
                'availability_basis':'퇴원 시점 대용값 · 실제 코드 등록 시점 미제공',
                'reaction_candidate':bool(REACTION.search(title)), 'confounder':bool(CONFOUNDER.search(title)),
                'drug_link':'unlinked', 'source':source(row,'hosp_diagnoses_icd')})
        labs = []
        sql = '''SELECT x.*, d.label, d.fluid FROM hosp_labevents x LEFT JOIN hosp_d_labitems d
                 ON x.itemid=d.itemid WHERE x.subject_id=?'''
        for record in conn.execute(sql, (sid,)):
            row = dict(record)
            if row.get('fluid') != 'Blood' or not LABS.search(row.get('label') or ''):
                continue
            if not visible(row, cutoff):
                excluded['labs'] += 1
                continue
            labs.append({'label':row['label'], 'time':row['charttime'], 'available_at':row['storetime'],
                'hadm_id':row['hadm_id'], 'value':row['value'], 'unit':row['valueuom'],
                'lower':row['ref_range_lower'], 'upper':row['ref_range_upper'], 'flag':row['flag'],
                'source':source(row,'hosp_labevents')})
        vitals = []
        for row in rows(conn, 'icu_chartevents', sid):
            label = item_map.get(row['itemid'],{}).get('label','')
            if label not in VITAL_LABELS:
                continue
            if not visible(row, cutoff):
                excluded['vitals'] += 1
                continue
            vitals.append({'label':label,'time':row['charttime'],'available_at':row['storetime'],
                'hadm_id':row['hadm_id'],'value':row['value'],'unit':row['valueuom'],
                'source':source(row,'icu_chartevents')})
        exposures.sort(key=lambda r:(r['time'],r['kind'],r['source']['row']))
        bundle = {'subject_id':sid, 'cutoff':cutoff, 'version':rev, 'prepared_at':now(),
            'admissions':admissions,'exposures':exposures,'diagnoses':diagnoses,
            'labs':labs,'vitals':vitals,'excluded':excluded,'notes_available':False,
            'coverage':{'prescriptions':'available' if not cutoff else 'excluded_no_available_timestamp',
                'inputevents':'retrospective' if not cutoff else 'excluded_final_interval_state',
                'diagnoses':'retrospective' if not cutoff else 'discharge_time_proxy'}}
        with conn:
            conn.execute('INSERT OR REPLACE INTO prepared VALUES (?,?,?,?,?)',
                         (sid,cutoff,rev,bundle['prepared_at'],json.dumps(bundle,ensure_ascii=False)))
        return bundle
    finally:
        if owned:
            conn.close()


def get_prepared(conn, sid, cutoff=''):
    row = conn.execute('SELECT bundle FROM prepared WHERE subject_id=? AND cutoff=? AND version=?',
                       (sid,cutoff_value(cutoff),version(conn))).fetchone()
    return json.loads(row[0]) if row else None


def review_drug(bundle, drug):
    key = normalize_drug(drug)
    matching = [r for r in bundle['exposures'] if r['key']==key]
    relevant = [r for r in bundle['diagnoses'] if r['reaction_candidate'] or r['confounder']]
    # These are patient-level diagnoses, NOT causally attributed to the queried drug.
    counts = {status:sum(r['status']==status for r in matching) for status in
              ['administration_recorded','infusion_recorded','ordered_only','not_given','uncertain']}
    context = []
    administration = [r for r in matching if r['status'] in {'administration_recorded','infusion_recorded'}]
    for vital in bundle['vitals']:
        try:
            t = datetime.fromisoformat(vital['time'])
        except ValueError:
            continue
        for exp in administration:
            if vital['hadm_id'] != exp['hadm_id']:
                continue
            delta = (t-datetime.fromisoformat(exp['time'])).total_seconds()/60
            if -60 <= delta <= 60:
                context.append({**vital,'minutes_from_exposure':round(delta,1),
                                'exposure_source':exp['source']})
                break
    return {'drug':drug, 'subject_id':bundle['subject_id'], 'cutoff':bundle['cutoff'],
            'version':bundle['version'],'prepared_at':bundle['prepared_at'],
            'allergy_status':'unable_to_ascertain', 'drug_causality':'not_assessed',
            'message':'서술기록과 알레르기 목록이 없어 약물 알레르기를 판단할 수 없습니다.',
            'counts':counts, 'total_exposures':len(matching), 'exposures':matching,
            'patient_context':relevant,'vital_context':context,
            'context_window':'투약 기록 전후 60분 · 탐색용 창이며 진단 기준 아님',
            'excluded':bundle['excluded'],'coverage':bundle['coverage']}
