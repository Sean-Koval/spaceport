'use strict';
const assert=require('node:assert/strict');
const {test}=require('node:test');
const crypto=require('node:crypto');
const runtime=require('../../cube/reviewed-postgres/security');
const contract={schema_version:1,adapter_revision:'cube-rest-v2',data_cube:'Bookings',
  members:{'Bookings.total':{kind:'measure',type:'number'},'Bookings.category':{kind:'dimension',type:'string'},
    'Bookings.day':{kind:'time',type:'time'},'Bookings.tenant':{kind:'dimension',type:'string'},
    'Bookings.group':{kind:'dimension',type:'string'},'Bookings.loaded_at':{kind:'dimension',type:'string'}},
  tenant_member:'Bookings.tenant',access_group_member:'Bookings.group',time_member:'Bookings.day',checkpoint_member:'Bookings.loaded_at'};
function canon(v){return typeof v!=='object'||v===null?JSON.stringify(v):'{'+Object.keys(v).sort().map(k=>JSON.stringify(k)+':'+canon(v[k])).join(',')+'}';}
const now=1700000000;
const options={now,bundleDigest:'a'.repeat(64),endpointContext:'consumer',contract};
function context(patch={}){return {iss:'dpg-gateway',aud:'dpg-cube',iat:now,exp:now+60,principal_id:'north-user',tenant_id:'north',policy_revision:1,
  model_bundle_digest:'a'.repeat(64),context:'consumer',adapter_revision:'cube-rest-v2',contract_digest:crypto.createHash('sha256').update(canon(contract)).digest('hex'),
  row_constraints:[{member:'Bookings.tenant',operator:'eq',values:['north']},{member:'Bookings.group',operator:'in',values:['north-A']}],...patch};}
function query(){return {measures:['Bookings.total'],dimensions:['Bookings.category','Bookings.loaded_at'],timeDimensions:[{dimension:'Bookings.day',granularity:'day'}],filters:[],limit:1001,timezone:'UTC'};}
test('valid reviewed query retains inline checkpoint and independently ANDs scope',()=>{
  const q=query();q.filters=[{or:[{member:'Bookings.tenant',operator:'equals',values:['south']},{member:'Bookings.category',operator:'notSet'}]}];
  const result=runtime.rewrite(q,context(),options);
  assert.equal(result.filters[0].and.length,3);assert.deepEqual(result.filters[0].and[1].values,['north']);
  assert.deepEqual(result.filters[0].and[2].values,['north-A']);assert.equal(q.filters.length,1);
});
test('context cannot select policy, contract, roles, tenant, expiry, or foreign bundle',()=>{
  for(const patch of [{roles:['admin']},{contract},{tenant_id:'south'},{exp:now+61},{contract_digest:'b'.repeat(64)},
    {model_bundle_digest:'b'.repeat(64)},{adapter_revision:'cube-rest-v1'},{row_constraints:context().row_constraints.slice(0,1)}])
    assert.throws(()=>runtime.validateContext(context(patch),options));
});
test('query cannot escape exact reviewed members, grouping or checkpoint',()=>{
  for(const patch of [{dimensions:['Bookings.category']},{dimensions:['Bookings.tenant','Bookings.loaded_at']},
    {measures:['Orders.revenue']},{measures:[{expression:'sql'}]},{dimensions:['Bookings.category','Bookings.loaded_at','Bookings.loaded_at']},
    {timeDimensions:[]},{timeDimensions:[{dimension:'Bookings.day',granularity:'month'}]},
    {filters:[{member:'Bookings.loaded_at',operator:'equals',values:['now']}]},{joins:[]},{limit:1002},{ungrouped:true}])
    assert.throws(()=>runtime.rewrite({...query(),...patch},context(),options));
});
test('scope partitions entitlements and principal but not token renewal',()=>{
  assert.equal(runtime.scopeId(context(),options),runtime.scopeId(context({iat:now-1,exp:now+59}),options));
  assert.notEqual(runtime.scopeId(context(),options),runtime.scopeId(context({principal_id:'narrow-user'}),options));
  const c=context();c.row_constraints[1].values=['north-B'];assert.notEqual(runtime.scopeId(context(),options),runtime.scopeId(c,options));
});
test('local malformed contract cannot broaden authority',()=>{
  for(const changed of [{...contract,checkpoint_member:'Bookings.category'},{...contract,tenant_member:'Bookings.group'},
    {...contract,members:{...contract.members,'Other.secret':{kind:'dimension',type:'string'}}}])
    assert.throws(()=>runtime.rewrite(query(),context(),{...options,contract:changed}));
});
