// Deterministic actual container MCP client. Model/attachment acceptance is separate.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { Client } from '/app/node_modules/@modelcontextprotocol/sdk/dist/esm/client/index.js';
import { StdioClientTransport } from '/app/node_modules/@modelcontextprotocol/sdk/dist/esm/client/stdio.js';
import { loadConfig } from '/app/src/config.ts';
import { resolvePluginServer } from '/app/src/plugin-mcp.ts';

const root = '/workspace/agent';
const mode = process.argv[2];
const server = resolvePluginServer(loadConfig().mcpServers.meal_concierge);
assert(server && server.type !== 'http');
assert(!JSON.stringify(server).includes('${PLUGIN_ROOT}'), 'native plugin expansion was skipped');
assert(!fs.existsSync('/var/run/docker.sock'));
assert.deepEqual(fs.readdirSync('/workspace/extra/meal-concierge-socket').sort(), ['service.sock', 'service.sock.owner.lock']);
assert(fs.existsSync('/home/node/.claude/skills/meal-concierge/SKILL.md'), 'native skill missing');
const client = new Client({ name: 'mc08-native-sdk', version: '1' });
await client.connect(new StdioClientTransport(server));
const call = async (name: string, args = {}) => {
  const result = await client.callTool({ name: `meal_concierge_${name}`, arguments: args });
  assert(!result.isError, JSON.stringify(result));
  const parsed = JSON.parse((result.content as any)[0].text);
  assert.deepEqual(result.structuredContent, parsed);
  return parsed;
};
try {
  const schema = await client.listTools();
  const names = schema.tools.map(t => t.name).sort();
  const expected = JSON.parse(fs.readFileSync('/workspace/extra/probes/schema.json', 'utf8'));
  assert.deepEqual(names, expected, 'package and current service source tool names differ');
  const status = await call('status');
  assert.equal(status.currency, 'SEK');
  if (mode === 'restart') {
    await call('setup', { action: 'apply', keep_current: true });
    await call('profile', { action: 'update', changes: { meals: { portions: 3 } } });
    fs.writeFileSync(`${root}/restart-request`, 'same MCP transport remains connected');
    const deadline = Date.now() + 20000;
    while (!fs.existsSync(`${root}/restart-ready`)) {
      assert(Date.now() < deadline, 'service restart did not complete');
      await new Promise(r => setTimeout(r, 50));
    }
    // The same stdio bridge and SDK connection now open the replacement socket.
    assert.equal((await call('profile', { action: 'show' })).profile.meals.portions, 3);
    assert.equal((await call('status')).household, status.household);
    const saved = await call('recipe_write', {
      recipe: JSON.parse(fs.readFileSync('/workspace/extra/probes/recipe.json', 'utf8')),
      idempotency_key: 'mc08-interactive',
    });
    fs.writeFileSync(`${root}/recipe.json`, JSON.stringify(saved.recipe));
  } else {
    assert.equal((await call('profile', { action: 'show' })).profile.meals.portions, 3);
    const saved = await call('recipe_write', {
      recipe: JSON.parse(fs.readFileSync('/workspace/extra/probes/recipe.json', 'utf8')),
      idempotency_key: mode === 'scheduled' ? 'mc08-occurrence' : 'mc08-interactive',
    });
    const ref = `${root}/${mode === 'scheduled' ? 'scheduled' : 'recipe'}.json`;
    if (mode === 'scheduled' && !fs.existsSync(ref)) fs.writeFileSync(ref, JSON.stringify(saved.recipe));
    const original = JSON.parse(fs.readFileSync(ref, 'utf8'));
    assert.equal(saved.recipe.id, original.id);
    assert.equal(saved.recipe.revision, original.revision);
    const got = await call('recipes', { action: 'get', recipe_id: original.id, revision: original.revision });
    assert.equal(got.recipe.id, original.id);
  }
  fs.appendFileSync(`${root}/results.jsonl`, JSON.stringify({ mode, household: status.household, tools: names.length }) + '\n');
  if (mode === 'scheduled' && !fs.existsSync(`${root}/dispatched`)) {
    fs.writeFileSync(`${root}/dispatched`, 'response received; native task acknowledgment pending');
    await new Promise(r => setTimeout(r, 25000));
  }
} finally {
  await client.close();
}
console.log(JSON.stringify({ wakeAgent: false }));
