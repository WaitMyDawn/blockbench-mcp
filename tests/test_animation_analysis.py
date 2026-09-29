import copy
import math
import pytest
from mcp.server.fastmcp.exceptions import ToolError
from blockbench_mcp import animation_analysis as aa, tools
from blockbench_mcp.codecs import bedrock, geckolib
from blockbench_mcp.errors import StateError, ValidationError
from blockbench_mcp.session import session


def bone(angle=0, x=0):
    return {"uuid": "bone", "name": "root", "position": [x, 0, 0], "rotation": [0, 0, angle],
            "quaternion": [0, 0, math.sin(math.radians(angle)/2), math.cos(math.radians(angle)/2)], "scale": [1, 1, 1]}


def test_quaternion_pose_equivalence_and_velocity_wrap():
    a, b = {"time": 0, "bones": [bone()]}, {"time": 1, "bones": [bone(360)]}
    assert aa.pose_delta(a, b)["pose_matches"]
    assert not aa.pose_delta(a, {"bones": [bone(x=.1)]})["pose_matches"]
    states = [{"time": t, "bones": [bone(r)]} for t,r in [(0,179),(1,-179),(0,0),(1,2)]]
    assert aa.velocity_delta(*states)["rotation_degrees_per_second"] == 0


def test_native_proof_requires_current_sync(project):
    session.project = project
    with pytest.raises(StateError): aa.native_proof()
    session.source_token = "stamp"
    session.synced_revision = session.revision
    assert aa.native_proof()["expect_elements"] == 0
    session.edited()
    with pytest.raises(StateError): aa.native_proof()


@pytest.mark.parametrize("bad", [math.nan, math.inf, -1])
def test_bounds_margin_rejects_nonfinite_and_negative(bad):
    with pytest.raises(ValidationError): aa.visible_bounds({"min": [0,0,0], "max": [1,1,1]}, bad)


def test_bounds_are_conservative_and_written_by_both_exporters(project, tmp_path):
    raw = {"min": [-100,-8,-180], "max": [120,160,60]}
    bounds = aa.visible_bounds(raw, 16)
    assert bounds == {"visible_bounds_width": 25, "visible_bounds_height": 13, "visible_bounds_offset": [0,4.75,0]}
    desc = bedrock.geometry_json(project, bounds)["minecraft:geometry"][0]["description"]
    assert all(desc[k] == v for k,v in bounds.items())
    import json
    result = geckolib.export_geckolib(project, str(tmp_path), render_bounds=bounds)
    desc = json.loads(__import__('pathlib').Path(result['written'][0]).read_text())["minecraft:geometry"][0]["description"]
    assert all(desc[k] == v for k,v in bounds.items())


def test_review_samples_keys_left_limits_contacts_and_seams_without_edits(project, monkeypatch):
    g = project.add_group("root")
    a = project.add_animation("idle", length=1, loop="loop")
    project.add_keyframe(a.uuid, g.uuid, "position", .375, [0,0,0], interpolation="step")
    project.add_animation("land", length=1)
    baseline = copy.deepcopy(project)
    requested = []
    def sample(batch, **kwargs):
        requested.extend(batch)
        return {"viewport": {"revision": 0}, "samples": [dict(r, bones=[bone(x=0 if r['animation']=='idle' else 2*r['time'])],
                bounds={"min": [-r['time'],0,0], "max": [1,2+r['time'],1]}, below_floor=[]) for r in batch]}
    monkeypatch.setattr(aa, "sample_native", sample)
    report = aa.review_native(project, transitions=[{"from":"idle","to":"land"}],sample_rate=4)
    assert any(r['time']==.375 for r in requested)
    assert any(abs(r['time']-(.375-1e-5))<1e-9 for r in requested)
    assert report['loops']['idle']['pose_matches']
    assert report['transitions'][0]['pose_matches']
    assert report['transitions'][0]['velocity_difference']['position_per_second']==2
    assert report['bounds']=={"min": [-1,0,0], "max": [1,3,1]}
    assert project.__dict__==baseline.__dict__


@pytest.mark.parametrize("args", [{"animations":[]},{"animations":["missing"]}, {"animations":["idle","idle"]},
                                  {"sample_rate":math.nan},{"sample_rate":61},{"sample_rate":0},
                                  {"transitions":[{"from":"idle","to":"missing"}]}])
def test_invalid_review_plan_never_contacts_bridge(project, monkeypatch, args):
    project.add_animation('idle', length=1)
    monkeypatch.setattr(aa,'sample_native',lambda *a,**k: pytest.fail('must preflight'))
    with pytest.raises(ValidationError): aa.review_native(project, **args)


def test_export_native_failure_preserves_files(project, tmp_path, monkeypatch):
    session.project=project
    path=tmp_path/'kept.geo.json';path.write_text('keep')
    monkeypatch.setattr(aa, 'review_native', lambda *a,**k: (_ for _ in ()).throw(StateError('stale')))
    with pytest.raises(ToolError): tools.export_model('bedrock',str(path),bounds_mode='animated')
    assert path.read_text()=='keep'
    assert not (tmp_path/'kept.geo.animation.json').exists()


def test_export_animated_bounds_passed_to_codec(project,tmp_path,monkeypatch):
    import json
    session.project=project
    bounds=aa.visible_bounds({'min':[-200,-10,-200],'max':[200,250,200]})
    monkeypatch.setattr(aa,'review_native',lambda *a,**k: dict(sample_count=10,sample_rate=12,bounds={},render_bounds=bounds,bounds_margin=16,limitations=[]))
    path=tmp_path/'boss.geo.json'
    data=tools.export_model('bedrock',str(path),bounds_mode='animated')['data']
    assert data['animated_bounds']['sample_count']==10
    desc=json.loads(path.read_text())['minecraft:geometry'][0]['description']
    assert all(desc[k]==v for k,v in bounds.items())


def test_render_native_atomic_file_and_fixed_camera(project,tmp_path,monkeypatch):
    from PIL import Image
    project.add_animation('fly',length=1)
    monkeypatch.setattr(aa,'native_proof',lambda:{'expect_revision':2})
    bounds={'min':[0,0,0],'max':[10,10,10]}
    monkeypatch.setattr(aa,'review_native',lambda *a,**k:dict(bounds=bounds,limitations=[]))
    captures=[]
    class Driver:
        fail=False
        def __init__(self,**kwargs):pass
        def capture(self,path,params):
            captures.append(params)
            if self.fail:raise StateError('stale')
            Image.new('RGB',(96,64),(int(params['time']*200),10,30)).save(path)
    monkeypatch.setattr(aa,'RemoteDriver',Driver)
    target=tmp_path/'test.gif'
    report=aa.render_native(project,['fly','fly'],str(target),fps=2,size=64,loop=True)
    assert report['frames']==4 and report['duration_ms']==2000
    assert all(c['fit_bounds']==bounds and c['expect_revision']==2 for c in captures)
    with Image.open(target) as im: assert im.n_frames==4 and im.info['loop']==0
    before=target.read_bytes()
    Driver.fail=True
    with pytest.raises(StateError):aa.render_native(project,['fly'],str(target),fps=2,size=64)
    assert target.read_bytes()==before
    assert list(tmp_path.iterdir())==[target]


@pytest.mark.parametrize('kwargs',[{'fps':0},{'fps':math.nan},{'size':900},{'camera':'unknown'}])
def test_render_limits_preflight(project,tmp_path,kwargs):
    with pytest.raises(ValidationError):aa.render_native(project,['fly'],str(tmp_path/'test.gif'),**kwargs)
