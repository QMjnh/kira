const {test} = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const {SHA256,fileSHA256} = require('../web/sha256.js');

test('incremental SHA-256 matches independent reference across block boundaries', () => {
  for (const size of [0,1,55,56,63,64,65,127,128,129,1024,1024*1024+17]) {
    const data = crypto.randomBytes(size);
    const expected = crypto.createHash('sha256').update(data).digest('hex');
    for (const chunk of [1,17,64,65537]) {
      const hash = new SHA256();
      for (let offset=0;offset<size;offset+=chunk) hash.update(data.subarray(offset,offset+chunk));
      assert.equal(hash.hex(),expected,`${size} bytes / ${chunk} chunk`);
      assert.equal(hash.hex(),expected);
      assert.throws(() => hash.update(new Uint8Array()),/finalized/);
    }
  }
});

test('file checksum reads bounded slices and includes every byte', async () => {
  const data = crypto.randomBytes(2*1024*1024+99);
  const progress = [];
  const file = new Blob([data]);
  assert.equal(await fileSHA256(file,x => progress.push(x)),crypto.createHash('sha256').update(data).digest('hex'));
  assert.deepEqual(progress,[1024*1024,2*1024*1024,data.length]);
  assert.equal(await fileSHA256(new Blob()),crypto.createHash('sha256').digest('hex'));
});

test('large-file length encoding remains correct beyond 512 MiB', () => {
  const block=Buffer.alloc(1024*1024,0x6b);
  const hash=new SHA256(), reference=crypto.createHash('sha256');
  for (let i=0;i<513;i++) { hash.update(block);reference.update(block); }
  const tail=Buffer.from('large-file tail');
  hash.update(tail);reference.update(tail);
  assert.equal(hash.hex(),reference.digest('hex'));
});
