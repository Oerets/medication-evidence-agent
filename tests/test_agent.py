import json
import unittest
from unittest.mock import patch
from app.agent import EvidenceTools, run_agent, validate_output


def bundle():
    return {'subject_id':'patient-a','cutoff':'','version':'test:1','prepared_at':'now',
        'exposures':[{'key':'drug-a','drug':'Drug-A','time':'2100-01-01 12:00:00',
            'hadm_id':'visit-a','status':'ordered_only','kind':'prescription','details':[],
            'source':{'table':'hosp_prescriptions','row':1}}],
        'diagnoses':[{'reaction_candidate':True,'confounder':False,'title':'Anaphylaxis',
            'drug_link':'unlinked','source':{'table':'hosp_diagnoses_icd','row':2}}],
        'labs':[],'vitals':[],'coverage':{},'excluded':{}}


class HarnessTests(unittest.TestCase):
    def test_unknown_tool_and_extra_scope_arguments_rejected(self):
        tools=EvidenceTools(bundle(),'Drug-A')
        for name,args in [('shell',{}),('read_medications',{'subject_id':'other'}),
                          ('read_source',{'evidence_id':'hosp_prescriptions:99'}),
                          ('read_medications',{'offset':-1})]:
            with self.assertRaises(ValueError):tools.call(name,args)

    def test_exact_drug_and_noncausal_context_preserved(self):
        tools=EvidenceTools(bundle(),'Different')
        self.assertEqual(tools.call('read_medications',{})['total'],0)
        d=tools.call('read_context',{})['diagnoses'][0]
        self.assertEqual(d['drug_link'],'unlinked')

    def test_fabricated_citation_rejected(self):
        with self.assertRaises(ValueError):
            validate_output({'summary':'x','findings':[{'text':'x','evidence_ids':['invented:1']}],
                'limitations':[]},{})

    @patch('app.agent.status',return_value={'digest':'test-digest'})
    def test_real_tool_loop_and_structured_output(self,_):
        calls=[]
        def chat(body):
            calls.append(body)
            if 'format' in body:
                return {'message':{'content':json.dumps({'summary':'검토 초안',
                    'findings':[{'text':'처방 기록','evidence_ids':['hosp_prescriptions:1']}],
                    'limitations':['인과성 미평가']})}}
            if len(calls)==1:
                return {'message':{'role':'assistant','content':'','tool_calls':[
                    {'function':{'name':n,'arguments':{}}} for n in ['read_medications','read_context']]}}
            return {'message':{'role':'assistant','content':'done'}}
        result=run_agent(bundle(),'Drug-A',chat=chat)
        self.assertEqual(len(result['trace']),2)
        self.assertTrue(any(m['role']=='tool' for m in calls[-1]['messages']))
        self.assertEqual(result['causality'],'not_assessed')
        self.assertTrue(result['review_required'])

    def test_model_refusal_is_bounded_and_never_fabricates_success(self):
        calls=[]
        def chat(body):
            calls.append(body)
            return {'message':{'role':'assistant','content':'I will not use tools'}}
        with self.assertRaises(ValueError):run_agent(bundle(),'Drug-A',chat=chat)
        self.assertEqual(len(calls),5)

    def test_invalid_medication_page_does_not_count_as_required_tool(self):
        tools=EvidenceTools(bundle(),'Drug-A')
        with self.assertRaises(ValueError):tools.call('read_medications',{'offset':-1})
        self.assertNotIn('read_medications',tools.called)


if __name__=='__main__':unittest.main()
