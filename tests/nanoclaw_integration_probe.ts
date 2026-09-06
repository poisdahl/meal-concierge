// Runs in a fresh pinned NanoClaw checkout; actual native template/group/runner.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { initDb, runMigrations, closeDb, getAgentGroup, getSession } from './src/db/index.ts';
import './src/mailbox/compose.ts';
import './src/cli/resources/groups.ts';
import './src/cli/resources/tasks.ts';
import { startCliServer, stopCliServer } from './src/cli/socket-server.ts';
import { materializeContainerJson } from './src/container-config.ts';
import { resolveSession, writeSessionContext, withMailboxSession } from './src/session-manager.ts';
import { getAgentMailbox } from './src/mailbox/index.ts';
import { buildMounts, composeSessionSpec } from './src/container-runner.ts';
import { DockerSessionDriver } from './src/drivers/docker-driver.ts';
import { mountPolicy } from './src/drivers/index.ts';
import { realCli, type SupervisedProcess } from './src/drivers/cli.ts';
import { LABELS } from './src/drivers/types.ts';
import { _resetStuckProcessingRowsForTesting } from './src/reconcile-session.ts';

const exec = promisify(execFile);
const scratch = process.env.MC08_SCRATCH!;
const phase = process.env.MC08_PHASE!;
const install = process.env.NANOCLAW_INSTALL_ID!;
assert(/^[a-z0-9-]{1,20}$/.test(install));
const handles: any[] = [];
const stopped = new Set<any>();
const helpers: { process: SupervisedProcess; exited: boolean; done: Promise<void> }[] = [];
const docker = realCli(path.join(scratch, 'bin/docker'));
const driver = new DockerSessionDriver({ ...mountPolicy(), cli: { ...docker, start(args, opts) {
  const process = docker.start(args, opts);
  let resolve!: () => void;
  const entry = { process, exited: false, done: new Promise<void>(r => { resolve = r; }) };
  process.onExit(() => { entry.exited = true; resolve(); });
  helpers.push(entry);
  return process;
} },
  networkArgsFor: () => ['--network', 'none'] });
async function cli(...args: string[]) {
  const result = await exec('node', ['--import', 'tsx', 'src/cli/client.ts', ...args, '--json']);
  const value = JSON.parse(result.stdout);
  assert(value.ok, result.stdout);
  return value.data;
}
async function until(check: () => any, message: string, timeout = 20000) {
  const deadline = Date.now() + timeout;
  while (!(await check())) {
    assert(Date.now() < deadline, message);
    await new Promise(r => setTimeout(r, 50));
  }
}
async function launch(groupId: string, session: any) {
  const group = (await getAgentGroup(groupId))!;
  const key = { agentGroupId: groupId, sessionId: session.id };
  const mailbox = getAgentMailbox();
  mailbox.prepare(key);
  writeSessionContext(groupId, session.id, await mailbox.runnerContext(key));
  const config = await materializeContainerJson(groupId);
  const mounts = await buildMounts(group, session, config, 'claude', {});
  const spec = composeSessionSpec({ agentGroup: group, session, containerName: `${install}-${session.id}`,
    containerConfig: config, mounts, contribution: {}, gateway: {},
    mailboxEnvironment: await mailbox.runnerEnvironment(key) });
  assert(`${install}-${session.id}`.length <= 48, 'native driver would hash this name outside the task claim');
  const name = `ncl-${install}-${session.id}`;
  const attempt = randomUUID();
  await exec(path.join(scratch, 'bin/docker'), ['inspect', name, '--format', '{{.Id}}']).then(() => {
    throw new Error('refusing existing container collision');
  }, error => {
    assert(/no such (object|container)/i.test(String(error.stderr)), 'could not check container collision');
  });
  fs.appendFileSync(path.join(scratch, 'container-names.jsonl'), JSON.stringify({ name, session: session.id, attempt }) + '\n');
  const handle = await driver.prepare(spec);
  assert.equal(handle.name, name);
  handles.push(handle);
  // Parent fallback reconciles only this journal, including a failed prepare.
  await handle.start();
  await until(async () => (await handle.status()).phase === 'running', 'container failed to start');
  const { stdout } = await exec(path.join(scratch, 'bin/docker'), ['inspect', handle.name]);
  const item = JSON.parse(stdout)[0];
  assert.equal(item.Config.Labels[LABELS.install], install);
  assert.equal(item.Config.Labels[LABELS.role], 'agent');
  fs.appendFileSync(path.join(scratch, 'containers.jsonl'), JSON.stringify({ id: item.Id, name: handle.name, session: session.id, attempt }) + '\n');
  assert.equal(item.HostConfig.NetworkMode, 'none');
  assert(item.HostConfig.CapDrop.includes('ALL'));
  assert(!item.Mounts.some((m: any) => m.Source.endsWith('/docker.sock') || m.Source === path.join(scratch, 'household')));
  assert(item.Mounts.filter((m: any) => m.Destination.startsWith('/workspace/extra')).every((m: any) => !m.RW));
  assert(!item.Config.Env.some((v: string) => /(?:TOKEN|SECRET|PASSWORD|API_KEY)=.+/i.test(v)));
  return handle;
}
async function inside(handle: any, ...args: string[]) {
  return exec(path.join(scratch, 'bin/docker'), ['exec', handle.name, ...args]);
}
async function stop(handle: any) {
  if (stopped.has(handle)) return;
  await handle.stop('MC08 isolated test');
  await until(async () => !(await exec(path.join(scratch, 'bin/docker'), ['ps', '-aq', '--filter', `name=^/${handle.name}$`])).stdout.trim(), 'owned container did not disappear');
  stopped.add(handle);
}

let failure = false;
try {
  const db = await initDb();
  await runMigrations(db, undefined, { mode: 'migrate' });
  await startCliServer();
  let owner: any;
  if (phase === 'first') {
    owner = await cli('groups', 'create', '--template', 'meal-concierge', '--name', 'MC08 owner', '--yes');
    assert(!owner.templateReport?.length, JSON.stringify(owner.templateReport));
    fs.writeFileSync(path.join(scratch, 'owner.json'), JSON.stringify(owner));
    const attachment = JSON.parse(fs.readFileSync(path.join(scratch, 'package/attachment.json'), 'utf8'));
    const mounts = [...attachment.additionalMounts, { hostPath: path.join(scratch, 'probes'), containerPath: 'probes', readonly: true }];
    const allowlist = path.join(process.env.HOME!, '.config/nanoclaw/mount-allowlist.json');
    fs.mkdirSync(path.dirname(allowlist), { recursive: true });
    fs.writeFileSync(allowlist, JSON.stringify({ allowedRoots: mounts.map(m => ({ path: m.hostPath, allowReadWrite: false })), blockedPatterns: [] }));
    for (const m of mounts) await cli('groups', 'config', 'add-mount', '--id', owner.id, '--host', m.hostPath, '--container', m.containerPath, '--ro');
  } else {
    owner = JSON.parse(fs.readFileSync(path.join(scratch, 'owner.json'), 'utf8'));
  }
  const first = (await resolveSession(owner.id, null, null, 'shared')).session;
  const a = await launch(owner.id, first);
  const groupDir = path.join(process.cwd(), 'groups', owner.folder);
  if (phase === 'first') {
    const client = inside(a, 'bun', '/workspace/extra/probes/client.ts', 'restart');
    // Attach rejection immediately; a failed client must not be unhandled while
    // the host waits for its restart signal.
    let clientError: unknown;
    const clientResult = client.then(value => ({ value }), error => { clientError = error; return { error }; });
    await until(() => {
      if (clientError) throw clientError;
      return fs.existsSync(path.join(groupDir, 'restart-request'));
    }, 'client did not request restart');
    fs.writeFileSync(path.join(scratch, 'restart-request'), a.name);
    await until(() => fs.existsSync(path.join(scratch, 'restart-ready')), 'parent did not restart Application');
    fs.writeFileSync(path.join(groupDir, 'restart-ready'), 'service ready');
    const result = await clientResult;
    if ('error' in result) throw result.error;
    const denied = await exec(path.join(scratch, 'bin/docker'), ['exec', '--user', `65534:${process.getgid!()}`, a.name,
      '/workspace/extra/meal-concierge-python/bin/python3.12', '-B', '-c',
      'import socket; s=socket.socket(socket.AF_UNIX); s.connect("/workspace/extra/meal-concierge-socket/service.sock"); print("connected",flush=True); s.sendall(b\'{"operation":"status"}\\n\'); print(s.recv(4096))',
    ]).then(r => r.stdout, e => String(e.stdout) + String(e.stderr));
    assert(denied.includes('connected') && /reset|b''/i.test(denied), denied);
    const unrelated = await cli('groups', 'create', '--name', 'MC08 unrelated', '--folder', 'mc08-unrelated');
    const other = await launch(unrelated.id, (await resolveSession(unrelated.id, null, null, 'shared')).session);
    await inside(other, 'bash', '-c', 'test ! -e /workspace/extra/meal-concierge-socket && test ! -e /var/run/docker.sock');
    await stop(other);
  } else {
    await inside(a, 'bun', '/workspace/extra/probes/client.ts', 'host-restart');
  }
  await stop(a);
  if (phase === 'first') {
    const second = (await resolveSession(owner.id, null, null, 'shared')).session;
    assert.notEqual(second.id, first.id);
    const b = await launch(owner.id, second);
    await inside(b, 'bun', '/workspace/extra/probes/client.ts', 'new-session');
    await stop(b);
  } else {
    const task = await cli('tasks', 'create', '--group', owner.id, '--name', 'MC08 occurrence',
      '--prompt', 'Synthetic script only; do not wake model or send messages.', '--process-after', new Date().toISOString(),
      '--script', 'exec bun /workspace/extra/probes/client.ts scheduled');
    const session = (await getSession(task.session_id))!;
    const c = await launch(owner.id, session);
    await until(() => fs.existsSync(path.join(groupDir, 'dispatched')), 'scheduled script did not complete MCP request');
    await stop(c);
    let retry: any;
    await withMailboxSession(owner.id, session.id, async mailbox => {
      _resetStuckProcessingRowsForTesting(mailbox, mailbox, session, 'MC08 own script lost acknowledgment');
      retry = mailbox.getTask(task.row_id);
    });
    assert.equal(retry.id, task.row_id);
    assert.equal(retry.tries, 1);
    await new Promise(r => setTimeout(r, Math.max(0, Date.parse(retry.processAfter) - Date.now()) + 100));
    const d = await launch(owner.id, session);
    await until(async () => withMailboxSession(owner.id, session.id, async mailbox => {
      mailbox.applyProcessingAcks(mailbox.getTerminalProcessingAcks());
      return mailbox.getTask(task.row_id)?.status === 'completed';
    }), 'retried occurrence was not completed');
    await stop(d);
  }
  console.log(JSON.stringify({ phase, native_template: true, same_container_socket_reconnect: phase === 'first', deterministic_sdk: true }));
} catch (error) {
  console.error(error);
  failure = true;
} finally {
  for (const handle of handles.reverse()) {
    try { await stop(handle); } catch (error) { console.error(error); failure = true; }
  }
  await stopCliServer();
  await closeDb();
  for (const entry of helpers) if (!entry.exited) entry.process.kill();
  await until(() => helpers.every(entry => entry.exited), 'owned Docker helpers survived shutdown', 5000).catch(error => {
    console.error(error); failure = true;
  });
  process.exit(failure ? 1 : 0);
}
