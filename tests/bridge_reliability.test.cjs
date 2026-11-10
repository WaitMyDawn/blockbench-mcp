// Exercise the actual plugin functions with asynchronous native API doubles.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../plugin/blockbench_mcp_bridge.js'), 'utf8');

function bridge(options = {}) {
    let seq = 0;
    const events = [];
    const c = vm.createContext({setTimeout, clearTimeout, console, Uint8Array,
        btoa: s => Buffer.from(s, 'binary').toString('base64'),
        Plugin: {register() {}}, Blockbench: {version: 'fake', Project: null},
        ModelProject: {all: []}, Formats: {bedrock: {}}, Format: {id: 'bedrock'},
        Cube: {all: []}, Texture: {all: []}, Canvas: {updateAll() {events.push('render');}}});
    const model = {meta: {model_format: 'bedrock'}, elements: [{type: 'cube', value: 1}], textures: [],
                   mcp_sync: {source_token: 'fresh', project_id: 'document', revision: 2}};
    function project(name, value = 0, saved = true) {
        const p = {uuid: 'p' + (++seq), name, save_path: 'D:/models/rig.bbmodel', saved,
            cubes: [{value}], textures: [],
            select() {c.Blockbench.Project = this; c.Cube.all = this.cubes; c.Texture.all = this.textures;},
            async close(force) {
                events.push('close-start:' + this.uuid);
                await new Promise(r => setTimeout(r, 15));
                c.ModelProject.all = c.ModelProject.all.filter(x => x !== this);
                events.push('close-end:' + this.uuid);
                return true;
            }};
        c.ModelProject.all.push(p); p.select(); return p;
    }
    const old = project('old', 0, !options.unsaved);
    c.Blockbench.read = (paths, opts, callback) => setTimeout(() => callback([
        {path: paths[0], content: options.invalid ? '{bad json' : JSON.stringify(model)}]), 5);
    c.Codecs = {project: {async load(data, file) {
        events.push('load');
        if (options.asyncLoad) await new Promise(r => setTimeout(r, 30));
        const p = project('new', data.elements[0].value);
        if (options.textureFailure) p.textures = [{img: {complete: false, naturalWidth: 0}}];
        if (options.delayedTexture) {
            const img = {complete: false, naturalWidth: 0};
            p.textures = [{img}];
            setTimeout(() => {img.complete = true; img.naturalWidth = 16;}, 60);
        }
        p.select();
        if (options.throwLoad) throw new Error('parse failure');
    }}};
    if (options.textureFailure || options.delayedTexture) model.textures = [{}];
    vm.runInContext(source, c);
    c.capturePngBytes = () => Buffer.from([137,80,78,71,13,10,26,10,c.Cube.all[0].value]);
    return {c, old, model, events};
}

test('same path/same count loads a new version and awaits asynchronous close', async () => {
    const b = bridge({asyncLoad: true});
    const result = await b.c.openProject(b.old.save_path, {expect_source_token: 'fresh'});
    assert.equal(result.ok, true);
    assert.notEqual(result.uuid, b.old.uuid);
    assert.equal(result.revision, 2);
    assert.equal(b.c.Cube.all[0].value, 1);
    assert.equal(b.c.ModelProject.all.length, 1);
    assert.ok(b.events.indexOf('load') < b.events.indexOf('close-start:' + b.old.uuid));
    assert.ok(b.events.includes('close-end:' + b.old.uuid));
});

test('unsaved same-path project is protected until explicit replacement', async () => {
    const b = bridge({unsaved: true});
    const refused = await b.c.openProject(b.old.save_path, {});
    assert.equal(refused.ok, false);
    assert.match(refused.error, /unsaved/);
    assert.equal(b.c.Blockbench.Project, b.old);
    assert.equal(b.events.length, 0);
    assert.equal((await b.c.openProject(b.old.save_path, {replace_unsaved: true})).ok, true);
});

test('invalid input and mismatched file tokens never close the old project', async () => {
    for (const options of [{invalid: true}, {}]) {
        const b = bridge(options);
        const result = await b.c.openProject(b.old.save_path, {expect_source_token: 'wrong'});
        assert.equal(result.ok, false);
        assert.equal(b.c.Blockbench.Project, b.old);
        assert.equal(b.c.ModelProject.all.length, 1);
        assert.equal(b.events.length, 0);
    }
});

test('partially failed load restores the old active tab', async () => {
    const b = bridge({throwLoad: true});
    const result = await b.c.openProject(b.old.save_path, {});
    assert.equal(result.ok, false);
    assert.equal(b.c.Blockbench.Project, b.old);
    assert.equal(b.c.ModelProject.all.length, 1);
    assert.ok(!b.events.includes('close-start:' + b.old.uuid));
});

test('texture timeout rolls back; delayed textures are awaited before old tab removal', async () => {
    const bad = bridge({textureFailure: true});
    const result = await bad.c.openProject(bad.old.save_path, {ready_timeout_ms: 40});
    assert.equal(result.ok, false);
    assert.match(result.error, /readiness/);
    assert.equal(bad.c.Blockbench.Project, bad.old);
    assert.equal(bad.c.ModelProject.all.length, 1);
    const good = bridge({delayedTexture: true});
    assert.equal((await good.c.openProject(good.old.save_path, {ready_timeout_ms: 500})).ok, true);
});

test('capture pixels and provenance agree and stale revision is rejected', async () => {
    const b = bridge();
    await b.c.openProject(b.old.save_path, {});
    const params = {expect_revision: 2, expect_project_id: 'document', expect_source_token: 'fresh'};
    const shot = b.c.captureCommand(params);
    assert.equal(shot.ok, true);
    assert.equal(shot.viewport.revision, 2);
    assert.equal(Buffer.from(shot.png_base64, 'base64')[8], 1);
    assert.equal(b.c.captureCommand({...params, expect_revision: 1}).ok, false);
    b.c.Blockbench.Project.saved = false;
    assert.equal(b.c.captureCommand(params).ok, false);
});

test('queued capture waits for an in-flight open operation', async () => {
    const b = bridge({asyncLoad: true});
    const plugin = {};
    const opened = b.c.queueCommand(plugin, 'open', {path: b.old.save_path});
    const capture = b.c.queueCommand(plugin, 'capture', {expect_revision: 2, expect_project_id: 'document'});
    const [a, shot] = await Promise.all([opened, capture]);
    assert.equal(a.ok, true);
    assert.equal(shot.ok, true);
    assert.equal(shot.viewport.uuid, a.uuid);
});

// Native transform/API doubles exercise restoration and protocol boundaries;
// interpolation and camera projection are additionally checked on real 5.2.1.
function animationBridge() {
    const b = bridge(), c = b.c;
    class Vector {
        constructor(x=0,y=0,z=0,w) {Object.assign(this,{x,y,z}); if(w!==undefined)this.w=w;}
        clone(){return new Vector(this.x,this.y,this.z,this.w);}
        copy(v){Object.assign(this,v);return this;}
        toArray(){return this.w===undefined?[this.x,this.y,this.z]:[this.x,this.y,this.z,this.w];}
    }
    class Box {
        constructor(){this.min=new Vector(Infinity,Infinity,Infinity);this.max=new Vector(-Infinity,-Infinity,-Infinity);}
        setFromObject(m){this.min.copy(m.low);this.max.copy(m.high);return this;}
        union(b){for(const k of ['x','y','z']){this.min[k]=Math.min(this.min[k],b.min[k]);this.max[k]=Math.max(this.max[k],b.max[k]);}return this;}
        isEmpty(){return this.max.x<this.min.x;}
    }
    const mesh=()=>({position:new Vector(1,2,3), quaternion:new Vector(0,0,0,1),scale:new Vector(1,1,1),visible:true,
        rotation:{toArray(){return [0,0,0,'ZYX'];}},low:new Vector(-1,-1,-1),high:new Vector(1,1,1),
        getWorldScale(v){return v.copy(this.scale);}});
    const g={uuid:'g',name:'root',mesh:mesh()};
    const cube={uuid:'cube',name:'foot',export:true,mesh:mesh()};
    c.Group={all:[g]}; c.Cube.all=[cube];c.THREE={Box3:Box,Vector3:Vector};
    c.Canvas.scene={traverse(fn){[g.mesh,cube.mesh].forEach(fn);},updateMatrixWorld(){}};
    c.Timeline={time:.25,playing:false};
    const a={name:'idle',uuid:'a',length:1,playing:'locked',animators:{g:{displayFrame(){
        g.mesh.position.x=c.Timeline.time*10;
        if(b.throwFrame)throw new Error('frame failure');
    }}}};
    c.Animation={all:[a],selected:null};
    c.Animator={resetLastValues(){},showDefaultPose(){g.mesh.position.x=0;}};
    return {...b,a,g,cube};
}

test('formal native sampling works with eval disabled and restores mesh/playhead/selection', () => {
    const b=animationBridge();
    const before=JSON.stringify(b.g.mesh.position);
    const result=b.c.animatedOperation({samples:[{animation:'idle',time:.8}],include_bones:true},false);
    assert.equal(result.ok,true);
    assert.equal(result.samples[0].bones[0].position[0],8);
    assert.equal(result.samples[0].below_floor[0].name,'foot');
    assert.equal(JSON.stringify(b.g.mesh.position),before);
    assert.equal(b.c.Timeline.time,.25);
    assert.equal(b.c.Animation.selected,null);
    assert.equal(b.a.playing,'locked');
    assert.equal(b.c.Blockbench.Project.saved,true);
});

test('invalid time and ambiguous animation fail before changing native state', () => {
    const b=animationBridge();
    for(const time of [-1,2,NaN,Infinity,'0']) {
        assert.equal(b.c.animatedOperation({animation:'idle',time},false).ok,false);
        assert.equal(b.g.mesh.position.x,1);
        assert.equal(b.c.Timeline.time,.25);
    }
    b.c.Animation.all.push({...b.a,uuid:'other'});
    assert.equal(b.c.animatedOperation({animation:'idle',time:0},false).ok,false);
    b.c.Timeline.playing=true;
    assert.match(b.c.animatedOperation({animation:'a',time:0},false).error,/pause/);
});

test('partial pose failure restores transforms and queued later operation still runs', async () => {
    const b=animationBridge();
    b.a.animators.g.displayFrame=()=>{b.g.mesh.position.x=999;throw new Error('frame failure');};
    const result=await b.c.queueCommand({allowEval:false},'animation_sample',{animation:'idle',time:.8});
    assert.equal(result.ok,false);
    assert.equal(b.g.mesh.position.x,1);
    assert.equal(b.c.Timeline.time,.25);
    assert.equal(b.c.captureCommand({}).ok,true);
});

test('sampling excludes nonexported subtrees/collapsed FX but preserves planar cubes', () => {
    const b=animationBridge();
    b.cube.parent={export:false};
    assert.equal(b.c.animatedOperation({animation:'idle',time:0},false).samples[0].bounds,null);
    b.cube.parent=null;b.cube.mesh.scale=new b.c.THREE.Vector3(.00001,.00001,.00001);
    assert.equal(b.c.animatedOperation({animation:'idle',time:0},false).samples[0].bounds,null);
    b.cube.mesh.scale=new b.c.THREE.Vector3(1,1,0);
    const result=b.c.animatedOperation({animation:'idle',time:0,contact_exclude_prefixes:['foot']},false);
    assert.ok(result.samples[0].bounds);
    assert.equal(result.samples[0].contact_bounds,null);
    assert.equal(result.samples[0].below_floor.length,0);
});

test('fresh format-plugin dirty flag is accepted only while full serialized content matches load baseline', () => {
    const b=bridge(), p=b.c.Blockbench.Project;
    p.saved=false;p.mcp_bridge_sync={project_id:'document',revision:2};
    b.c.Codecs.project.compile=()=>({asset:b.c.Cube.all[0].value});
    p.mcp_bridge_load_baseline=b.c.nativeDocumentContent();
    const params={expect_project_id:'document',expect_revision:2};
    assert.equal(b.c.captureCommand(params).viewport.load_baseline_unchanged,true);
    b.c.Cube.all[0].value=7;
    assert.equal(b.c.captureCommand(params).ok,false);
    assert.equal(p.saved,false);
});

async function scopeBridge() {
    const {c, old} = bridge();
    const actions = {}, listeners = {};
    let seq = 0;
    class Vector {
        constructor(x = 0, y = 0, z = 0) { this.values = [x, y, z]; }
        fromArray(values) { this.values = Array.from(values); return this; }
        toArray() { return this.values.slice(); }
        copy(vector) { return this.fromArray(vector.values); }
    }
    class Box {
        constructor(min = new Vector(Infinity, Infinity, Infinity), max = new Vector(-Infinity, -Infinity, -Infinity)) {
            Object.assign(this, {min, max});
        }
        setFromPoints(points) {
            return points.reduce((box, point) => box.union(new Box(point, point)), this);
        }
        union(other) {
            this.min = new Vector(...this.min.values.map((lo, i) => Math.min(lo, other.min.values[i])));
            this.max = new Vector(...this.max.values.map((hi, i) => Math.max(hi, other.max.values[i])));
            return this;
        }
        containsBox(other) {
            return this.min.values.every((lo, i) => lo <= other.min.values[i] && other.max.values[i] <= this.max.values[i]);
        }
        intersectsBox(other) {
            return this.min.values.every((lo, i) => lo <= other.max.values[i] && other.min.values[i] <= this.max.values[i]);
        }
        getCenter(out) { return out.fromArray(this.min.values.map((lo, i) => (lo + this.max.values[i]) / 2)); }
        getSize(out) { return out.fromArray(this.min.values.map((lo, i) => this.max.values[i] - lo)); }
    }
    class Group {
        constructor() { this.children = []; }
        add(item) { this.children.push(item); item.parent = this; }
        removeFromParent() { this.parent.children.splice(this.parent.children.indexOf(this), 1); }
        traverse(callback) { callback(this); this.children.forEach(callback); }
    }
    class Resource {
        setFromPoints(points) { this.points = points; return this; }
        dispose() { this.disposed = true; }
    }
    class Mesh {
        constructor(geometry, material) {
            Object.assign(this, {geometry, material, position: new Vector()});
        }
    }
    class Outline extends Mesh {
        constructor() { super(new Resource(), new Resource()); }
    }
    Object.assign(c, {
        guid: () => 'scope-' + (++seq),
        THREE: {Vector3: Vector, Box3: Box, Group, Mesh, Box3Helper: Outline, BoxGeometry: Resource,
            MeshBasicMaterial: Resource, LineLoop: Mesh, BufferGeometry: Resource, LineBasicMaterial: Resource},
        Action: class { constructor(id, spec) { actions[id] = spec; } },
        MenuBar: {menus: {tools: {addAction() {}}}},
        Dialog: class { constructor(options) { this.options = options; } show() {} },
    });
    c.Canvas.gizmos = [];
    c.Canvas.scene = new Group();
    c.Blockbench.on = (name, callback) => { listeners[name] = callback; };
    c.Codecs.project.compile = () => ({elements: c.Cube.all.map(cube => [cube.uuid, cube.value])});
    await c.openProject(old.save_path, {});
    const project = c.Blockbench.Project;
    const inside = {uuid: 'foot', value: 1, mesh: {updateWorldMatrix() {}}, getGlobalVertexPositions: () => [[0, 0, 0], [2, 2, 2]]};
    const crossing = {uuid: 'belt', value: 2, mesh: {updateWorldMatrix() {}}, getGlobalVertexPositions: () => [[0, 1, 0], [2, 4, 2]]};
    const texture = {uuid: 'atlas'};
    function face(uv) {
        return {uv, getTexture: () => texture, getVertexIndices: () => [0, 1, 0, 1]};
    }
    inside.name = 'Foot';
    crossing.name = 'Belt';
    inside.faces = {north: face([0, 0, 2, 2]), west: face([2, 0, 4, 2])};
    crossing.faces = {south: face([8, 8, 12, 12])};
    project.getUVWidth = project.getUVHeight = () => 16;
    c.Cube.all.splice(0, 1, inside, crossing);
    project.mcp_bridge_load_baseline = c.nativeDocumentContent();
    c.Cube.selected = [inside];
    const plugin = {};
    c.registerScopeActions(plugin);
    return {c, project, inside, crossing, actions, listeners, plugin};
}

test('fixed manual scope is a disposable viewport gizmo, independent of native selection', async () => {
    const {c, project, actions, listeners, plugin} = await scopeBridge();
    const savedModel = c.nativeDocumentContent();
    actions.mcp_scope_select.click();
    const overlay = plugin.scopeOverlay;
    assert.equal(overlay.no_export, true);
    assert.deepEqual(c.Canvas.scene.children, [overlay]);
    assert.deepEqual(c.Canvas.gizmos, [overlay]);
    c.Cube.selected = [];
    const selected = await c.runCommand(plugin, 'scope_get', {expect_project_id: 'document', expect_revision: 2});
    assert.equal(selected.scope.confirmed, true);
    assert.deepEqual(Array.from(selected.scope.uuids), ['foot']);
    assert.equal(c.nativeDocumentContent(), savedModel);
    assert.equal(project.saved, true);
    listeners.unselect_project();
    assert.deepEqual(c.Canvas.scene.children, []);
    assert.deepEqual(c.Canvas.gizmos, []);
    assert.equal(overlay.children[0].geometry.disposed, true);
    listeners.select_project();
    assert.equal(c.Canvas.scene.children.length, 1);
    actions.mcp_scope_clear.click();
    assert.deepEqual(c.Canvas.scene.children, []);
    assert.equal(c.nativeDocumentContent(), savedModel);
});

test('proposal requires native confirmation and an unchanged model', async () => {
    const {c, inside, actions, plugin} = await scopeBridge();
    const params = {expect_project_id: 'document', expect_revision: 2, expect_source_token: 'fresh'};
    const response = await c.runCommand(plugin, 'scope_propose', {...params, uuids: ['foot'], confirmed: true});
    assert.equal(response.scope.confirmed, false);
    actions.mcp_scope_confirm.click();
    const confirmed = await c.runCommand(plugin, 'scope_get', params);
    assert.equal(confirmed.scope.confirmed, true);
    assert.equal(confirmed.scope.id, response.scope.id);
    inside.value = 99;
    assert.equal((await c.runCommand(plugin, 'scope_get', params)).ok, false);
});

test('transparent box includes only wholly enclosed cubes and stays out of the saved model', async () => {
    const {c, actions, plugin} = await scopeBridge();
    const savedModel = c.nativeDocumentContent();
    actions.mcp_scope_region.click();
    const {onFormChange, onConfirm} = plugin.scopeDialog.options;
    const bounds = {min: [0, 0, 0], max: [2, 3, 2]};
    onFormChange(bounds);
    assert.equal(plugin.scopeOverlay.children.length, 4);
    assert.equal(plugin.editScope, undefined);
    onConfirm(bounds);
    const result = await c.runCommand(plugin, 'scope_get', {});
    assert.equal(result.scope.confirmed, true);
    assert.deepEqual(Array.from(result.scope.uuids), ['foot']);
    assert.equal(c.nativeDocumentContent(), savedModel);
});

test('manual face scope keeps explicit faces and includes unselected atlas footprints', async () => {
    const {c, inside, crossing, actions, plugin} = await scopeBridge();
    c.Cube.selected = [inside, crossing];
    const native = c.nativeDocumentContent();
    actions.mcp_scope_faces.click();
    plugin.scopeDialog.options.onConfirm({face_0: true, face_2: true});
    c.Cube.selected = [];
    const result = await c.runCommand(plugin, 'scope_get', {});
    assert.equal(result.scope.confirmed, true);
    assert.deepEqual(Array.from(result.scope.faces, ref => ref.face), ['north', 'south']);
    assert.equal(result.scope.footprints.length, 3);
    assert.equal(plugin.scopeOverlay.children.length, 4);
    assert.equal(c.nativeDocumentContent(), native);
    const overlay = plugin.scopeOverlay;
    actions.mcp_scope_clear.click();
    assert.equal(c.Canvas.scene.children.length, 0);
    assert.equal(overlay.children[0].geometry.disposed, true);
});

test('agent face proposal stays pending until the native confirmation action', async () => {
    const {c, inside, actions, plugin} = await scopeBridge();
    const proposal = await c.runCommand(plugin, 'scope_propose', {faces: [{uuid: inside.uuid, face: 'west'}]});
    assert.equal(proposal.scope.confirmed, false);
    actions.mcp_scope_confirm.click();
    assert.equal((await c.runCommand(plugin, 'scope_get', {})).scope.confirmed, true);
});

test('manual faces attach to an open unsaved project without a preceding MCP sync', async () => {
    const {c, project, inside, actions, plugin} = await scopeBridge();
    project.saved = false;
    delete project.mcp_bridge_sync;
    delete project.mcp_bridge_load_baseline;
    const content = c.nativeDocumentContent();
    assert.equal((await c.runCommand(plugin, 'scope_import', {})).scope, null);
    actions.mcp_scope_faces.click();
    plugin.scopeDialog.options.onConfirm({face_0: true});
    const result = await c.runCommand(plugin, 'scope_import', {});
    assert.equal(result.scope.faces[0].uuid, inside.uuid);
    assert.equal(result.stamp.revision, 0);
    assert.equal(JSON.stringify(result.model), content);
    assert.equal(project.saved, false);
    assert.equal(c.Blockbench.Project, project);
    const proof = Object.fromEntries(Object.entries(result.stamp).map(([key, value]) => ['expect_' + key, value]));
    assert.equal((await c.runCommand(plugin, 'scope_get', proof)).ok, true);
    inside.value++;
    assert.equal((await c.runCommand(plugin, 'scope_import', {})).ok, false);
});

test('UV panel locks native selected faces and draws persistent marks only on their atlas', async () => {
    const {c, inside, crossing, plugin} = await scopeBridge();
    class Node {
        constructor() { this.children = []; this.style = {}; this.classList = {toggle() {}}; this.events = {}; }
        addEventListener(name, action) { this.events[name] = action; }
        append(...nodes) { nodes.forEach(node => { this.children.push(node); node.parentNode = this; }); }
        prepend(node) { this.children.unshift(node); node.parentNode = this; }
        remove() { this.parentNode.children.splice(this.parentNode.children.indexOf(this), 1); }
    }
    const frame = new Node(), header = new Node();
    c.document = {createElement: () => new Node(), getElementById: () => header};
    c.Cube.selected = [inside, crossing];
    c.UVEditor = {
        getMappableElements: () => c.Cube.selected,
        getSelectedFaces: cube => cube === inside ? ['west'] : ['south'],
        texture: {uuid: 'atlas'},
        vue: {$refs: {frame}, toPixels: value => (value * 10) + 'px'},
    };
    c.refreshScopeUV(plugin);
    assert.equal(header.children[0].children[0].textContent, '锁定选面');
    header.children[0].children[0].events.click({stopPropagation() {}});
    const scope = (await c.runCommand(plugin, 'scope_get', {})).scope;
    assert.deepEqual(Array.from(scope.faces, ref => ref.face), ['west', 'south']);
    c.Cube.selected = [];
    c.refreshScopeUV(plugin);
    assert.equal(plugin.scopeUVBadge.textContent, '已锁定 2 面');
    assert.equal(plugin.scopeUVBadge.hidden, false);
    assert.equal(frame.children[0].children.length, 2);
    assert.equal(frame.children[0].children[0].style.left, '20px');
    assert.equal(frame.children[0].children[1].children[0].textContent, '南');
    c.UVEditor.texture = {uuid: 'different'};
    c.refreshScopeUV(plugin);
    assert.equal(frame.children.length, 0);
    assert.equal(plugin.scopeUVBadge.hidden, false);
    c.unregisterScopeUV(plugin);
    assert.equal(header.children.length, 0);
});
