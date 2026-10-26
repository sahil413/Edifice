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
