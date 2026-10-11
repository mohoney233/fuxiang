#!/usr/bin/env node
// Dump PE exports (name -> RVA) for a DLL. Usage: node pe_exports.js <dll> [outFile]
'use strict';
const fs = require('fs');
const file = process.argv[2];
const b = fs.readFileSync(file);
const peOff = b.readUInt32LE(0x3c);
const coff = peOff + 4;
const nSec = b.readUInt16LE(coff + 2);
const optSize = b.readUInt16LE(coff + 16);
const opt = coff + 20;
const magic = b.readUInt16LE(opt);
const is64 = magic === 0x20b;
const ddOff = opt + (is64 ? 112 : 96);
const expRva = b.readUInt32LE(ddOff);
const secs = [];
for (let i = 0; i < nSec; i++) {
  const s = opt + optSize + i * 40;
  secs.push({ vsize: b.readUInt32LE(s + 8), va: b.readUInt32LE(s + 12), raw: b.readUInt32LE(s + 20), rawSize: b.readUInt32LE(s + 16) });
}
const rva2off = rva => { for (const s of secs) if (rva >= s.va && rva < s.va + Math.max(s.vsize, s.rawSize)) return rva - s.va + s.raw; return -1; };
const lines = [];
if (expRva) {
  const eo = rva2off(expRva);
  const nNames = b.readUInt32LE(eo + 24);
  const fnRva = b.readUInt32LE(eo + 28);
  const nmRva = b.readUInt32LE(eo + 32);
  const ordRva = b.readUInt32LE(eo + 36);
  const fo = rva2off(fnRva), no = rva2off(nmRva), oo = rva2off(ordRva);
  for (let i = 0; i < nNames; i++) {
    const nr = b.readUInt32LE(no + i * 4);
    const nop = rva2off(nr);
    let end = nop; while (b[end]) end++;
    const ord = b.readUInt16LE(oo + i * 2);
    const rva = b.readUInt32LE(fo + ord * 4);
    lines.push(`${b.subarray(nop, end).toString('latin1')}\t0x${rva.toString(16)}`);
  }
}
console.log('exports:', lines.length);
const out = process.argv[3];
if (out) fs.writeFileSync(out, lines.join('\n'));
const want = ['PyGILState_Ensure','PyGILState_Release','PyRun_SimpleString','PyRun_String','PyImport_ImportModule','PyModule_GetDict','PyObject_CallMethod','PyObject_GetAttrString','Py_IsInitialized','Py_Initialize','PyEval_SaveThread','PyGILState_GetThisThreadState','PySys_SetObject','PyImport_AddModule','PyErr_Print','Py_Finalize'];
console.log('--- requested ---');
for (const w of want) {
  const hit = lines.find(l => l.startsWith(w + '\t'));
  console.log(`  ${w}\t${hit ? hit.split('\t')[1] : 'MISSING'}`);
}
