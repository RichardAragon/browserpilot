import io,json
from pilot.model import StructuredModel,object_schema,STRING
from pilot.store import Store
import pytest

def test_openai_transport_uses_fixed_model_endpoint_and_validated_structured_output(tmp_path,monkeypatch):
    s=Store(tmp_path);s.save_model({'provider':'openai','model':'test-model','api_key':'test-key'})
    seen={}
    def fake(req,timeout):
        seen['url']=req.full_url;seen['body']=json.loads(req.data);seen['authorization']=req.headers['Authorization']
        return io.BytesIO(json.dumps({'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':'{"message":"Ready"}'}]}],'usage':{'input_tokens':2}}).encode())
    monkeypatch.setattr('urllib.request.urlopen',fake)
    model=StructuredModel(s,tmp_path/'out');response=model.request('Return a structured answer',{'task':'Check model'},object_schema({'message':STRING}))
    assert response=={'message':'Ready'} and seen['url']=='https://api.openai.com/v1/responses'
    assert seen['body']['store'] is False and seen['body']['text']['format']['strict'] is True
    assert 'tools' not in seen['body'] and seen['authorization']=='Bearer test-key'
    assert 'test-key' not in (tmp_path/'out/call-00.meta.json').read_text()

@pytest.mark.parametrize('result',[{'status':'incomplete','output':[]},{'status':'completed','output':[{'type':'message','content':[{'type':'refusal','refusal':'No'}]}]},{'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':'{"message":"ok","extra":"unsafe"}'}]}]}])
def test_model_refusal_incomplete_and_extra_properties_do_not_become_actions(tmp_path,monkeypatch,result):
    s=Store(tmp_path);s.save_model({'provider':'openai','model':'test-model','api_key':'test-key'})
    monkeypatch.setattr('urllib.request.urlopen',lambda *a,**k:io.BytesIO(json.dumps(result).encode()))
    with pytest.raises(ValueError):StructuredModel(s,tmp_path/'out').request('Check',{},object_schema({'message':STRING}))
