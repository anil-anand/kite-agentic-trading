const { spawn } = require('child_process');

const pythonCode = `
import sys
import json

res = {"jsonrpc": "2.0", "result": ["a" * 100000], "id": 1}
output = json.dumps(res)
print(output, flush=True)
`
const child = spawn('python3', ['-c', pythonCode]);

let stdoutBuffer = '';
child.stdout.on('data', (data) => {
  stdoutBuffer += data.toString();
  const lines = stdoutBuffer.split('\n');
  stdoutBuffer = lines.pop() || '';
  for (const line of lines) {
    if (!line.trim()) continue;
    try {
      JSON.parse(line);
      console.log('Success parsed line of length', line.length);
    } catch (e) {
      console.error('Error parsing line:', line.substring(0, 100));
    }
  }
});
