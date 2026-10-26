// Firestore security-rules tests. Run inside the emulator:  npm run test:rules
import { test, before, after, beforeEach } from 'node:test';
import { readFileSync } from 'node:fs';
import { initializeTestEnvironment, assertSucceeds, assertFails } from '@firebase/rules-unit-testing';
import { setLogLevel, doc, getDoc, setDoc, updateDoc, deleteDoc, collection, collectionGroup, getDocs, writeBatch, serverTimestamp } from 'firebase/firestore';

const ADMIN = 'admin@example.com', EDITOR = 'editor@example.com', VIEWER = 'viewer@example.com', STRANGER = 'nobody@example.com';
let env;
const as = (email, verified = true) => env.authenticatedContext(email.split('@')[0], { email, email_verified: verified }).firestore();
const anon = () => env.unauthenticatedContext().firestore();
const ev = (by, extra = {}) => Object.assign({ event_id: 'e1', site_id: 's1', view: 'land', name: 'Site', type: 'note', ts: '2026-10-26T04:00:00.000Z', text: 'hello', by }, extra);

setLogLevel('silent');
before(async () => {
  env = await initializeTestEnvironment({ projectId: 'demo-edifice', firestore: { rules: readFileSync(new URL('../firestore.rules', import.meta.url), 'utf8') } });
});
after(async () => { await env.cleanup(); });
beforeEach(async () => {
  await env.clearFirestore();
  await env.withSecurityRulesDisabled(async (c) => {
    const db = c.firestore();
    await setDoc(doc(db, 'users', ADMIN), { role: 'admin', name: 'Admin' });
    await setDoc(doc(db, 'users', EDITOR), { role: 'editor', name: 'Editor' });
    await setDoc(doc(db, 'users', VIEWER), { role: 'viewer', name: 'Viewer' });
    await setDoc(doc(db, 'sites', 's1'), { view: 'land', name: 'Site', score: 70, j: '{}' });
    await setDoc(doc(db, 'meta', 'land'), { j: '{}' });
    await setDoc(doc(db, 'private', 'email_engine'), { js: '' });
    await setDoc(doc(db, 'learnings', 'land'), { md: '' });
    await setDoc(doc(db, 'deals', 's1'), { site_id: 's1', view: 'land', name: 'Site', stage: 'New', updated: new Date(), updated_by: ADMIN });
    await setDoc(doc(db, 'deals/s1/events', 'f1'), ev(ADMIN, { event_id: 'f1', type: 'followup', status: 'open', assignee: 'Any bot', due: '' }));
    await setDoc(doc(db, 'sites', 'm-done1'), { view: 'land', name: 'Manual', manual: true, status: 'enriched', input: '1 Main St', created_by: EDITOR, created: new Date(), j: '{}' });
    await setDoc(doc(db, 'rsites', 'm-secret1'), { view: 'land', name: 'Secret', manual: true, restricted: true, status: 'enriched', input: 'x', created_by: ADMIN, created: new Date(), j: '{"price":1}' });
    await setDoc(doc(db, 'rdeals', 'm-secret1'), { site_id: 'm-secret1', view: 'land', name: 'Secret', stage: 'Under Contract', updated: new Date(), updated_by: ADMIN });
    await setDoc(doc(db, 'rdeals/m-secret1/revents', 'r1'), ev(ADMIN, { event_id: 'r1', site_id: 'm-secret1', type: 'followup', status: 'open', assignee: 'Any bot', due: '2026-11-11' }));
  });
});

test('signed-out and unlisted users read nothing', async () => {
  for (const db of [anon(), as(STRANGER)]) {
    for (const p of ['sites/s1', 'meta/land', 'private/email_engine', 'learnings/land', 'deals/s1', 'deals/s1/events/f1', `users/${ADMIN}`]) await assertFails(getDoc(doc(db, p)));
    await assertFails(getDocs(collection(db, 'sites')));
    await assertFails(getDocs(collectionGroup(db, 'events')));
  }
});

test('unverified email is refused even if listed', async () => {
  await assertFails(getDoc(doc(as(EDITOR, false), 'sites/s1')));
});

test('members of every role can read site data, deals, events, roster', async () => {
  for (const e of [ADMIN, EDITOR, VIEWER]) {
    const db = as(e);
    await assertSucceeds(getDocs(collection(db, 'sites')));
    await assertSucceeds(getDoc(doc(db, 'meta/land')));
    await assertSucceeds(getDoc(doc(db, 'private/email_engine')));
    await assertSucceeds(getDoc(doc(db, 'learnings/land')));
    await assertSucceeds(getDocs(collection(db, 'deals')));
    await assertSucceeds(getDocs(collectionGroup(db, 'events')));
    await assertSucceeds(getDocs(collection(db, 'users')));
  }
});

test('email case is normalised', async () => {
  await assertSucceeds(getDoc(doc(as('Editor@Example.com'), 'sites/s1')));
});

test('nobody can write pipeline data from a client', async () => {
  for (const e of [ADMIN, EDITOR]) {
    const db = as(e);
    await assertFails(setDoc(doc(db, 'sites/s2'), { view: 'land', j: '{}' }));
    await assertFails(updateDoc(doc(db, 'meta/land'), { j: 'x' }));
    await assertFails(setDoc(doc(db, 'private/email_engine'), { js: 'x' }));
    await assertFails(setDoc(doc(db, 'learnings/land'), { md: 'x' }));
  }
});

test('editors and admins write deals and events; viewers cannot', async () => {
  for (const e of [EDITOR, ADMIN]) {
    const db = as(e);
    const b = writeBatch(db);
    b.set(doc(db, 'deals/s1/events', 'st-' + e), ev(e, { event_id: 'st-' + e, type: 'stage', from_stage: 'New', to_stage: 'Reviewing', reasons: ['Power'], text: '' }));
    b.set(doc(db, 'deals', 's1'), { site_id: 's1', view: 'land', name: 'Site', stage: 'Reviewing', updated: serverTimestamp(), updated_by: e });
    await assertSucceeds(b.commit());
    await assertSucceeds(setDoc(doc(db, 'deals/s1/events', 'n-' + e), ev(e, { event_id: 'n-' + e })));
    await assertSucceeds(setDoc(doc(db, 'deals/s1/events', 'f-' + e), ev(e, { event_id: 'f-' + e, type: 'followup', status: 'open', assignee: 'Any bot', due: '2026-11-01' })));
  }
  const v = as(VIEWER);
  await assertFails(setDoc(doc(v, 'deals/s1/events', 'n-v'), ev(VIEWER, { event_id: 'n-v' })));
  await assertFails(setDoc(doc(v, 'deals', 's1'), { site_id: 's1', view: 'land', name: 'Site', stage: 'Passed', updated: serverTimestamp(), updated_by: VIEWER }));
  await assertFails(updateDoc(doc(v, 'deals/s1/events/f1'), { status: 'done', done_by: VIEWER, done_at: serverTimestamp() }));
  await assertFails(setDoc(doc(as(STRANGER), 'deals/s1/events', 'n-x'), ev(STRANGER, { event_id: 'n-x' })));
});

test('events must be authored as yourself and well-formed', async () => {
  const db = as(EDITOR);
  await assertFails(setDoc(doc(db, 'deals/s1/events', 'x1'), ev(ADMIN, { event_id: 'x1' })));                   // spoofed author
  await assertFails(setDoc(doc(db, 'deals/s1/events', 'x2'), ev(EDITOR, { event_id: 'other' })));               // id mismatch
  await assertFails(setDoc(doc(db, 'deals/s1/events', 'x3'), ev(EDITOR, { event_id: 'x3', site_id: 's9' })));   // wrong deal
  await assertFails(setDoc(doc(db, 'deals/s1/events', 'x4'), ev(EDITOR, { event_id: 'x4', type: 'stage', to_stage: 'Bogus', reasons: ['Price'] })));
  await assertFails(setDoc(doc(db, 'deals/s1/events', 'x5'), ev(EDITOR, { event_id: 'x5', type: 'stage', to_stage: 'Passed', reasons: [] })));  // a reason is required
  await assertFails(setDoc(doc(db, 'deals/s1/events', 'x6'), ev(EDITOR, { event_id: 'x6', text: '' })));        // empty note
  await assertFails(setDoc(doc(db, 'deals/s1/events', 'x7'), ev(EDITOR, { event_id: 'x7', type: 'followup', status: 'done' })));
  await assertFails(setDoc(doc(db, 'deals/s1/events', 'x8'), ev(EDITOR, { event_id: 'x8', role: 'admin' })));   // unknown field
  await assertFails(setDoc(doc(db, 'deals', 's1'), { site_id: 's1', view: 'land', name: 'Site', stage: 'Nope', updated: serverTimestamp(), updated_by: EDITOR }));
  await assertFails(setDoc(doc(db, 'deals', 's1'), { site_id: 's1', view: 'land', name: 'Site', stage: 'Passed', updated: serverTimestamp(), updated_by: ADMIN }));
});

test('history is append-only except follow-up status', async () => {
  const db = as(EDITOR);
  await assertSucceeds(updateDoc(doc(db, 'deals/s1/events/f1'), { status: 'done', done_by: EDITOR, done_at: serverTimestamp() }));
  await assertSucceeds(updateDoc(doc(db, 'deals/s1/events/f1'), { status: 'open', done_by: null, done_at: null }));
  await assertFails(updateDoc(doc(db, 'deals/s1/events/f1'), { text: 'rewritten' }));
  await assertFails(updateDoc(doc(db, 'deals/s1/events/f1'), { status: 'done', done_by: ADMIN }));
  await assertFails(deleteDoc(doc(db, 'deals/s1/events/f1')));
  await assertSucceeds(setDoc(doc(db, 'deals/s1/events', 'n1'), ev(EDITOR, { event_id: 'n1' })));
  await assertFails(updateDoc(doc(db, 'deals/s1/events/n1'), { status: 'done' }));                             // notes are not follow-ups
  await assertSucceeds(deleteDoc(doc(as(ADMIN), 'deals/s1/events/n1')));
});

test('only admins manage users', async () => {
  for (const e of [EDITOR, VIEWER, STRANGER]) {
    const db = as(e);
    await assertFails(setDoc(doc(db, 'users', 'new@example.com'), { role: 'viewer' }));
    await assertFails(updateDoc(doc(db, 'users', VIEWER), { role: 'admin' }));
    await assertFails(deleteDoc(doc(db, 'users', VIEWER)));
  }
  await assertFails(setDoc(doc(as(EDITOR), 'users', EDITOR), { role: 'admin' }));                              // no self-promotion
  const a = as(ADMIN);
  await assertSucceeds(setDoc(doc(a, 'users', 'new@example.com'), { role: 'viewer', name: 'New', added_by: ADMIN, added: serverTimestamp() }));
  await assertFails(setDoc(doc(a, 'users', 'Upper@example.com'), { role: 'viewer' }));                           // ids are lowercase
  await assertFails(setDoc(doc(a, 'users', 'x@example.com'), { role: 'owner' }));
  await assertSucceeds(updateDoc(doc(a, 'users', 'new@example.com'), { role: 'editor', updated_by: ADMIN, updated: serverTimestamp() }));
  await assertSucceeds(deleteDoc(doc(a, 'users', 'new@example.com')));
  await assertFails(updateDoc(doc(a, 'users', ADMIN), { role: 'viewer' }));                                     // cannot demote self
  await assertFails(deleteDoc(doc(a, 'users', ADMIN)));                                                         // cannot remove self
  await assertSucceeds(updateDoc(doc(a, 'users', ADMIN), { name: 'Renamed' }));
});

// ---- manual sites (Add site) ----
const stub = (by, extra = {}) => Object.assign({ view: 'land', name: 'New site', manual: true, status: 'pending', input: '100 Main St, Salina, KS', created_by: by, created: serverTimestamp() }, extra);

test('editors and admins can queue a manual site; viewers and strangers cannot', async () => {
  await assertSucceeds(setDoc(doc(as(EDITOR), 'sites', 'm-abc123'), stub(EDITOR)));
  await assertSucceeds(setDoc(doc(as(ADMIN), 'sites', 'm-abc124'), stub(ADMIN, { edits: { acres: 12 } })));
  await assertFails(setDoc(doc(as(VIEWER), 'sites', 'm-abc125'), stub(VIEWER)));
  await assertFails(setDoc(doc(as(STRANGER), 'sites', 'm-abc126'), stub(STRANGER)));
});

test('a queued site is only a pending stub: no record, no score, own author, manual id', async () => {
  const db = as(EDITOR);
  await assertFails(setDoc(doc(db, 'sites', 'm-x1'), stub(EDITOR, { j: '{}' })));                        // client cannot write the record
  await assertFails(setDoc(doc(db, 'sites', 'm-x2'), stub(EDITOR, { score: 99 })));
  await assertFails(setDoc(doc(db, 'sites', 'm-x3'), stub(EDITOR, { status: 'enriched' })));
  await assertFails(setDoc(doc(db, 'sites', 'm-x4'), stub(ADMIN)));                                       // spoofed author
  await assertFails(setDoc(doc(db, 'sites', 'm-x5'), stub(EDITOR, { manual: false })));
  await assertFails(setDoc(doc(db, 'sites', 'abc123'), stub(EDITOR)));                                    // not a manual id
  await assertFails(setDoc(doc(db, 'sites', 'm-x6'), stub(EDITOR, { input: '' })));
  await assertFails(setDoc(doc(db, 'sites', 'm-x7'), stub(EDITOR, { input: 'x'.repeat(2001) })));
  await assertFails(setDoc(doc(db, 'sites', 'm-x8'), stub(EDITOR, { edits: { score: 100 } })));           // only listed fields are editable
  await assertFails(setDoc(doc(db, 'sites', 'm-x9'), stub(EDITOR, { restricted: true })));
});

test('hand edits: only edits/status on manual docs, never pipeline docs', async () => {
  const db = as(EDITOR);
  const ed = (extra = {}) => Object.assign({ edits: { acres: 50.5, zoning: 'I-3' }, edited_by: EDITOR, edited: serverTimestamp(), status: 'pending' }, extra);
  await assertSucceeds(updateDoc(doc(db, 'sites/m-done1'), ed()));
  await assertFails(updateDoc(doc(db, 'sites/m-done1'), ed({ j: '{"score":100}' })));
  await assertFails(updateDoc(doc(db, 'sites/m-done1'), ed({ score: 100 })));
  await assertFails(updateDoc(doc(db, 'sites/m-done1'), ed({ edited_by: ADMIN })));
  await assertFails(updateDoc(doc(db, 'sites/m-done1'), ed({ edits: { owner: 'x', bogus: 1 } })));
  await assertFails(updateDoc(doc(db, 'sites/s1'), ed()));                                                  // pipeline site
  await assertFails(updateDoc(doc(as(VIEWER), 'sites/m-done1'), Object.assign(ed(), { edited_by: VIEWER })));
  await assertFails(deleteDoc(doc(db, 'sites/m-done1')));
  await assertFails(deleteDoc(doc(as(ADMIN), 'sites/s1')));                                                 // admins cannot delete pipeline sites
  await assertSucceeds(deleteDoc(doc(as(ADMIN), 'sites/m-done1')));
});

// ---- restricted (admin-only) sites ----
test('viewers and editors cannot read restricted sites, deals or events (get or list)', async () => {
  for (const e of [VIEWER, EDITOR, STRANGER]) {
    const db = as(e);
    await assertFails(getDoc(doc(db, 'rsites/m-secret1')));
    await assertFails(getDocs(collection(db, 'rsites')));
    await assertFails(getDoc(doc(db, 'rdeals/m-secret1')));
    await assertFails(getDocs(collection(db, 'rdeals')));
    await assertFails(getDoc(doc(db, 'rdeals/m-secret1/revents/r1')));
    await assertFails(getDocs(collection(db, 'rdeals/m-secret1/revents')));
    await assertFails(getDocs(collectionGroup(db, 'revents')));
  }
  // the members' normal queries still work and contain nothing restricted
  const ed = as(EDITOR);
  const sites = await getDocs(collection(ed, 'sites'));
  if (sites.docs.some(d => d.id === 'm-secret1' || d.data().restricted)) throw new Error('restricted site leaked into sites');
  const evs = await getDocs(collectionGroup(ed, 'events'));
  if (evs.docs.some(d => d.data().site_id === 'm-secret1')) throw new Error('restricted event leaked into events');
  const deals = await getDocs(collection(ed, 'deals'));
  if (deals.docs.some(d => d.id === 'm-secret1')) throw new Error('restricted deal leaked into deals');
});

test('viewers and editors cannot write restricted docs', async () => {
  for (const e of [VIEWER, EDITOR]) {
    const db = as(e);
    await assertFails(setDoc(doc(db, 'rsites', 'm-new1'), stub(e)));
    await assertFails(setDoc(doc(db, 'rdeals', 'm-secret1'), { site_id: 'm-secret1', view: 'land', name: 'Secret', stage: 'Passed', updated: serverTimestamp(), updated_by: e }));
    await assertFails(setDoc(doc(db, 'rdeals/m-secret1/revents', 'n-' + e), ev(e, { event_id: 'n-' + e, site_id: 'm-secret1' })));
    await assertFails(updateDoc(doc(db, 'rdeals/m-secret1/revents/r1'), { status: 'done', done_by: e, done_at: serverTimestamp() }));
  }
});

test('admins read and write restricted sites, deals and events', async () => {
  const db = as(ADMIN);
  await assertSucceeds(getDoc(doc(db, 'rsites/m-secret1')));
  await assertSucceeds(getDocs(collection(db, 'rsites')));
  await assertSucceeds(getDocs(collection(db, 'rdeals')));
  await assertSucceeds(getDocs(collectionGroup(db, 'revents')));
  await assertSucceeds(setDoc(doc(db, 'rsites', 'm-new2'), stub(ADMIN)));
  const b = writeBatch(db);
  b.set(doc(db, 'rdeals/m-secret1/revents', 'st1'), ev(ADMIN, { event_id: 'st1', site_id: 'm-secret1', type: 'stage', from_stage: 'LOI Submitted', to_stage: 'Under Contract', reasons: ['Price'], text: '' }));
  b.set(doc(db, 'rdeals', 'm-secret1'), { site_id: 'm-secret1', view: 'land', name: 'Secret', stage: 'Under Contract', updated: serverTimestamp(), updated_by: ADMIN });
  await assertSucceeds(b.commit());
  await assertSucceeds(updateDoc(doc(db, 'rdeals/m-secret1/revents/r1'), { status: 'done', done_by: ADMIN, done_at: serverTimestamp() }));
  await assertFails(setDoc(doc(db, 'rdeals/m-secret1/revents', 'bad'), ev(EDITOR, { event_id: 'bad', site_id: 'm-secret1' })));  // still authored as yourself
});

// ---- Phase 2: review queue, deal email log, learned weights
const rq = (who, extra = {}) => Object.assign({ event_id: 'st1', site_id: 's1', view: 'land', name: 'Site', track: 'power', from_stage: 'New', to_stage: 'Reviewing',
  who, reasons: ['Power'], text: 'why', ts: '2026-10-26T04:00:00.000Z', status: 'pending', created: serverTimestamp() }, extra);
const em = (acct, extra = {}) => Object.assign({ site_id: 's1', date: '2026-10-26T04:00:00Z', account: acct, from: acct, to: 'x@example.com', subject: 'Hi', excerpt: 'Short.',
  link: 'https://mail.google.com/mail/u/?authuser=' + acct + '#all/t1', thread_id: 't1', message_id: 'm1', dir: 'out', source: 'app', by: acct, created: serverTimestamp() }, extra);

test('review queue: editors file their own pending item, cannot read it; admins read and answer', async () => {
  await assertSucceeds(setDoc(doc(as(EDITOR), 'reviewQueue', 'st1'), rq(EDITOR)));
  await assertFails(getDoc(doc(as(EDITOR), 'reviewQueue', 'st1')));
  await assertFails(getDoc(doc(as(VIEWER), 'reviewQueue', 'st1')));
  await assertFails(getDocs(collection(as(EDITOR), 'reviewQueue')));
  await assertSucceeds(getDoc(doc(as(ADMIN), 'reviewQueue', 'st1')));
  await assertSucceeds(updateDoc(doc(as(ADMIN), 'reviewQueue', 'st1'), { status: 'answered', answer: 'Price too high', answered_at: serverTimestamp() }));
  await assertFails(updateDoc(doc(as(ADMIN), 'reviewQueue', 'st1'), { to_stage: 'Passed' }));
  await assertFails(updateDoc(doc(as(EDITOR), 'reviewQueue', 'st1'), { status: 'answered' }));
});

test('review queue: shape is enforced', async () => {
  const E = as(EDITOR);
  await assertFails(setDoc(doc(as(VIEWER), 'reviewQueue', 'st1'), rq(VIEWER)));
  await assertFails(setDoc(doc(E, 'reviewQueue', 'st1'), rq(ADMIN)));                       // impersonation
  await assertFails(setDoc(doc(E, 'reviewQueue', 'st2'), rq(EDITOR)));                      // id != event_id
  await assertFails(setDoc(doc(E, 'reviewQueue', 'st1'), rq(EDITOR, { status: 'answered' })));
  await assertFails(setDoc(doc(E, 'reviewQueue', 'st1'), rq(EDITOR, { track: 'storage' })));
  await assertFails(setDoc(doc(E, 'reviewQueue', 'st1'), rq(EDITOR, { answer: 'x' })));
  await assertFails(setDoc(doc(E, 'reviewQueue', 'st1'), rq(EDITOR, { restricted: true })));
  await assertSucceeds(setDoc(doc(as(ADMIN), 'reviewQueue', 'st1'), rq(ADMIN, { restricted: true })));
});

test('deal emails: admin only; app sends must be well-formed and from the signed-in admin', async () => {
  await env.withSecurityRulesDisabled(async (c) => {
    await setDoc(doc(c.firestore(), 'dealEmails', 's1'), { site_id: 's1', summary: 'quiet', counts: { in: 1, out: 2 } });
    await setDoc(doc(c.firestore(), 'dealEmails/s1/items', 't0_m0'), { subject: 'x' });
    await setDoc(doc(c.firestore(), 'config', 'senders'), { emails: [ADMIN, 'admin2@example.com'] });
    await setDoc(doc(c.firestore(), 'users', 'admin3@example.com'), { role: 'admin', name: 'Other admin' });
  });
  await assertFails(getDoc(doc(as(EDITOR), 'config', 'senders')));
  await assertSucceeds(getDoc(doc(as(ADMIN), 'config', 'senders')));
  await assertFails(setDoc(doc(as(ADMIN), 'config', 'senders'), { emails: ['x@example.com'] }));
  for (const u of [EDITOR, VIEWER]) {
    await assertFails(getDoc(doc(as(u), 'dealEmails', 's1')));
    await assertFails(getDoc(doc(as(u), 'dealEmails/s1/items', 't0_m0')));
    await assertFails(getDocs(collection(as(u), 'dealEmails/s1/items')));
    await assertFails(getDocs(collectionGroup(as(u), 'items')));
    await assertFails(setDoc(doc(as(u), 'dealEmails/s1/items', 't1_m1'), em(u)));
  }
  const A = as(ADMIN);
  await assertSucceeds(getDoc(doc(A, 'dealEmails', 's1')));
  await assertSucceeds(getDocs(collection(A, 'dealEmails/s1/items')));
  await assertSucceeds(setDoc(doc(A, 'dealEmails/s1/items', 't1_m1'), em(ADMIN)));
  await assertFails(setDoc(doc(A, 'dealEmails/s1/items', 't1_m2'), em(ADMIN)));                                   // id mismatch
  await assertFails(setDoc(doc(A, 'dealEmails/s1/items', 't1_m1'), em(ADMIN, { account: 'other@example.com', link: 'https://mail.google.com/mail/u/?authuser=other@example.com#all/t1' })));  // not an allowed sender
  await assertSucceeds(setDoc(doc(A, 'dealEmails/s1/items', 't2_m2'), em('admin2@example.com', { thread_id: 't2', message_id: 'm2', by: ADMIN, link: 'https://mail.google.com/mail/u/?authuser=admin2@example.com#all/t2' })));  // the other allowed account
  await assertFails(setDoc(doc(as('admin3@example.com'), 'dealEmails/s1/items', 't3_m3'), em('admin3@example.com', { thread_id: 't3', message_id: 'm3', link: 'https://mail.google.com/mail/u/?authuser=admin3@example.com#all/t3' })));  // admin but not a sender
  await assertFails(setDoc(doc(A, 'dealEmails/s1/items', 't1_m1'), em(ADMIN, { body: 'full text' })));              // no bodies
  await assertFails(setDoc(doc(A, 'dealEmails/s1/items', 't1_m1'), em(ADMIN, { link: 'https://evil.example.com' })));
  await assertFails(setDoc(doc(A, 'dealEmails/s1/items', 't1_m1'), em(ADMIN, { source: 'sync' })));
  await assertSucceeds(updateDoc(doc(A, 'dealEmails', 's1'), { last_touch: serverTimestamp(), last_out: serverTimestamp(), last_account: ADMIN, updated_by: ADMIN }));
  await assertFails(updateDoc(doc(A, 'dealEmails', 's1'), { summary: 'rewritten', updated_by: ADMIN, last_touch: serverTimestamp(), last_out: serverTimestamp() }));
});

test('learned weights: members read, nobody writes from the client', async () => {
  await env.withSecurityRulesDisabled(async (c) => { await setDoc(doc(c.firestore(), 'learnings', 'weights'), { version: 1, n: 0 }); });
  await assertSucceeds(getDoc(doc(as(VIEWER), 'learnings', 'weights')));
  for (const u of [VIEWER, EDITOR, ADMIN]) await assertFails(setDoc(doc(as(u), 'learnings', 'weights'), { version: 9 }));
  await assertFails(getDoc(doc(as(STRANGER), 'learnings', 'weights')));
});
