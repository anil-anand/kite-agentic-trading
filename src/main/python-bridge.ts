import { spawn, ChildProcess } from 'child_process';
import * as path from 'path';
import { app, webContents } from 'electron';
import { RPCRequest, RPCResponse, RPCEvent } from '../shared/types';
import * as channels from '../shared/ipc-channels';

class PythonBridge {
  private childProcess: ChildProcess | null = null;
  private requestId = 0;
  private pendingRequests: Map<number, { resolve: (value: any) => void; reject: (error: any) => void }> = new Map();
  private restartCount = 0;
  private maxRestarts = 3;
  private isShuttingDown = false;
  private pythonPath: string;
  private scriptPath: string;

  constructor() {
    const isPackaged = app.isPackaged;
    
    if (isPackaged) {
      // In production, use the PyInstaller standalone binary located in extraResources
      const platformBinary = process.platform === 'win32' ? 'kite_agent_backend.exe' : 'kite_agent_backend';
      this.pythonPath = path.join(process.resourcesPath, 'backend_dist', platformBinary);
      this.scriptPath = ''; // Not needed for binary
    } else {
      // In dev mode, let uv select and provision the project's environment.
      const basePath = path.join(__dirname, '..', '..', '..');
      this.pythonPath = 'uv';
      this.scriptPath = path.join(basePath, 'backend', 'main.py');
    }
  }

  public start(): void {
    if (this.childProcess) return;

    this.isShuttingDown = false;
    
    if (app.isPackaged) {
      console.log(`Starting Python backend binary at ${this.pythonPath}`);
      this.childProcess = spawn(this.pythonPath, [], { cwd: path.dirname(this.pythonPath) });
    } else {
      console.log(`Starting Python backend with uv at ${this.scriptPath}`);
      this.childProcess = spawn(this.pythonPath, ['run', '--locked', 'python', '-m', 'backend.main'], { cwd: path.dirname(path.dirname(this.scriptPath)) });
    }

    this.childProcess.stdout?.on('data', (data) => {
      const lines = data.toString().split('\n');
      for (const line of lines) {
        if (!line.trim()) continue;
        try {
          const parsed = JSON.parse(line);
          this.handlePythonMessage(parsed);
        } catch (e) {
          console.error('Error parsing Python output:', line);
        }
      }
    });

    this.childProcess.stderr?.on('data', (data) => {
      console.error(`Python Stderr: ${data.toString()}`);
    });

    this.childProcess.on('exit', (code, signal) => {
      console.log(`Python process exited with code ${code}, signal ${signal}`);
      this.childProcess = null;
      
      // Notify renderer
      this.broadcastToRenderer(channels.APP_PYTHON_STATUS, { running: false, error: `Exited with code ${code}` });

      if (!this.isShuttingDown && this.restartCount < this.maxRestarts) {
        this.restartCount++;
        console.log(`Restarting Python process (${this.restartCount}/${this.maxRestarts})...`);
        setTimeout(() => this.start(), 1000); // Wait a second before restart
      } else {
        console.error('Python process failed too many times or is shutting down.');
      }
    });

    this.childProcess.on('error', (err) => {
      console.error('Failed to start Python process:', err);
    });

    // Notify renderer
    this.broadcastToRenderer(channels.APP_PYTHON_STATUS, { running: true, error: null });
  }

  public stop(): void {
    this.isShuttingDown = true;
    if (this.childProcess) {
      this.childProcess.kill('SIGINT');
      setTimeout(() => {
        if (this.childProcess) {
          this.childProcess.kill('SIGKILL');
        }
      }, 5000);
    }
    
    // Reject all pending requests
    for (const [id, req] of this.pendingRequests.entries()) {
      req.reject(new Error('Python bridge shutting down'));
      this.pendingRequests.delete(id);
    }
  }

  public isRunning(): boolean {
    return this.childProcess !== null && !this.childProcess.killed;
  }

  public async call(method: string, params: Record<string, unknown> = {}): Promise<any> {
    if (!this.isRunning()) {
      throw new Error('Python process is not running');
    }

    return new Promise((resolve, reject) => {
      const id = ++this.requestId;
      this.pendingRequests.set(id, { resolve, reject });

      const request: RPCRequest = { id, method, params };
      const requestStr = JSON.stringify(request) + '\n';
      
      this.childProcess!.stdin?.write(requestStr);
    });
  }

  private handlePythonMessage(msg: any) {
    // If it has 'id', it's a response to a request
    if (msg && typeof msg.id === 'number') {
      const response = msg as RPCResponse;
      const pendingReq = this.pendingRequests.get(response.id);
      
      if (pendingReq) {
        if (response.error) {
          pendingReq.reject(response.error);
        } else {
          pendingReq.resolve(response.result);
        }
        this.pendingRequests.delete(response.id);
      }
    } 
    // If it has 'event', it's a push event
    else if (msg && typeof msg.event === 'string') {
      const eventMsg = msg as RPCEvent;
      // Broadcast to renderer
      let channel = eventMsg.event;
      // Map event names to channels if necessary, or assume they match
      this.broadcastToRenderer(channel, eventMsg.data);
    }
  }

  private broadcastToRenderer(channel: string, data: any) {
    const allWebContents = webContents.getAllWebContents();
    for (const contents of allWebContents) {
      contents.send(channel, data);
    }
  }
}

export const pythonBridge = new PythonBridge();
