import { spawn, ChildProcess } from 'child_process';
import * as path from 'path';
import { app, webContents } from 'electron';
import { RPCRequest, RPCResponse, RPCEvent } from '../shared/types';
import * as channels from '../shared/ipc-channels';
import { secureStorage } from './secure-storage';

class PythonBridge {
  private childProcess: ChildProcess | null = null;
  private requestId = 0;
  private pendingRequests: Map<number, { resolve: (value: any) => void; reject: (error: any) => void }> = new Map();
  private restartCount = 0;
  private maxRestarts = 3;
  private isShuttingDown = false;
  private backendReady = false;
  private handshakeStarted = false;
  private restartTimer: ReturnType<typeof setTimeout> | null = null;
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
    this.backendReady = false;
    this.handshakeStarted = false;
    if (this.restartTimer) clearTimeout(this.restartTimer);
    this.restartTimer = null;
    
    if (app.isPackaged) {
      console.log(`Starting Python backend binary at ${this.pythonPath}`);
      this.childProcess = spawn(this.pythonPath, [], { cwd: path.dirname(this.pythonPath) });
    } else {
      console.log(`Starting Python backend with uv at ${this.scriptPath}`);
      this.childProcess = spawn(this.pythonPath, ['run', '--locked', 'python', '-m', 'backend.main'], { cwd: path.dirname(path.dirname(this.scriptPath)) });
    }

    const child = this.childProcess;
    let stdoutBuffer = '';
    child.stdout?.on('data', (data) => {
      if (this.childProcess !== child || this.isShuttingDown) return;
      stdoutBuffer += data.toString();
      const lines = stdoutBuffer.split('\n');
      stdoutBuffer = lines.pop() || '';
      for (const line of lines) {
        if (!line.trim()) continue;
        try {
          const parsed = JSON.parse(line);
          this.handlePythonMessage(parsed, child);
        } catch (e) {
          console.error('Error parsing Python output:', line);
        }
      }
    });

    this.childProcess.stderr?.on('data', (data) => {
      console.error(`Python Stderr: ${data.toString()}`);
    });

    const handleExit = (code: number | null, signal: string | null) => {
      if (this.childProcess !== child) return;
      console.log(`Python process exited with code ${code}, signal ${signal}`);
      this.childProcess = null;
      this.backendReady = false;

      this.rejectPendingRequests(new Error(`Python backend exited with code ${code}`));
      
      // Notify renderer
      this.broadcastToRenderer(channels.APP_PYTHON_STATUS, { running: false, error: `Exited with code ${code}` });

      if (!this.isShuttingDown && this.restartCount < this.maxRestarts) {
        this.restartCount++;
        console.log(`Restarting Python process (${this.restartCount}/${this.maxRestarts})...`);
        this.restartTimer = setTimeout(() => {
          this.restartTimer = null;
          if (!this.isShuttingDown) this.start();
        }, 1000);
      } else {
        console.error('Python process failed too many times or is shutting down.');
      }
    };
    child.on('exit', handleExit);

    child.on('error', (err) => {
      if (this.childProcess !== child) return;
      console.error('Failed to start Python process:', err);
      this.backendReady = false;
      this.rejectPendingRequests(err);
      this.broadcastToRenderer(channels.APP_PYTHON_STATUS, { running: false, ready: false, error: err.message });
      // A failed spawn emits error/close but never exit. Release that child so
      // recovery does not get stuck behind a process that was never started.
      if (!child.pid) {
        handleExit(null, null);
      }
    });

    this.broadcastToRenderer(channels.APP_PYTHON_STATUS, { running: false, ready: false, error: null });
  }

  public stop(): void {
    this.isShuttingDown = true;
    if (this.restartTimer) clearTimeout(this.restartTimer);
    this.restartTimer = null;
    if (this.childProcess) {
      const child = this.childProcess;
      child.kill('SIGINT');
      setTimeout(() => {
        if (this.childProcess === child) {
          child.kill('SIGKILL');
        }
      }, 5000);
    }
    
    this.backendReady = false;
    this.rejectPendingRequests(new Error('Python bridge shutting down'));
  }

  public isRunning(): boolean {
    return this.childProcess !== null && !this.childProcess.killed && this.backendReady;
  }

  public async call(method: string, params: Record<string, unknown> = {}): Promise<any> {
    return this.callRpc(method, params, true);
  }

  private async callRpc(method: string, params: Record<string, unknown>, requireReady: boolean): Promise<any> {
    if (this.isShuttingDown || !this.childProcess || this.childProcess.killed || !this.childProcess.stdin?.writable || (requireReady && !this.backendReady)) {
      throw new Error(requireReady ? 'Python backend is not ready' : 'Python process is not running');
    }

    return new Promise((resolve, reject) => {
      const id = ++this.requestId;
      this.pendingRequests.set(id, { resolve, reject });

      const request: RPCRequest = { id, method, params };
      const requestStr = JSON.stringify(request) + '\n';
      
      this.childProcess!.stdin!.write(requestStr, (error) => {
        if (!error) return;
        this.pendingRequests.delete(id);
        reject(error);
      });
    });
  }

  private rejectPendingRequests(error: Error): void {
    for (const [id, req] of this.pendingRequests.entries()) {
      req.reject(error);
      this.pendingRequests.delete(id);
    }
  }

  private async rehydrateTrustedBackend(generation: string, child: ChildProcess): Promise<void> {
    const isCurrent = () => this.childProcess === child && !this.isShuttingDown;
    try {
      const credentials = secureStorage.loadCredentials();
      await this.callRpc('set_credentials', { credentials }, false);
      if (!isCurrent()) return;
      const session = await this.callRpc('check_session', {}, false);
      if (!isCurrent()) return;
      const supervision = await this.callRpc('resume_supervision', {}, false);
      if (!isCurrent()) return;
      this.backendReady = true;
      this.broadcastToRenderer(channels.APP_PYTHON_STATUS, {
        running: true,
        ready: true,
        tradingReady: session?.is_valid === true && supervision?.supervisionActive === true
          && !supervision?.reconciliationPending && !supervision?.lifecycleRecoveryPending
          && !supervision?.controlStateInvalid && !supervision?.protectionFailureHalt
          && !supervision?.hardFlattenReason,
        generation,
        sessionValid: session?.is_valid === true,
        supervision,
        error: null,
      });
    } catch (error: any) {
      if (!isCurrent()) return;
      this.backendReady = false;
      this.broadcastToRenderer(channels.APP_PYTHON_STATUS, {
        running: false,
        ready: false,
        generation,
        error: error?.message || 'Trusted backend rehydration failed',
      });
    }
  }

  private handlePythonMessage(msg: any, child: ChildProcess) {
    // If it has 'id', it's a response to a request
    if (msg && typeof msg.id === 'number') {
      const response = msg as RPCResponse;
      const pendingReq = this.pendingRequests.get(response.id);
      
      if (pendingReq) {
        if (response.error) {
          const errMsg = response.error.message || JSON.stringify(response.error);
          pendingReq.reject(new Error(errMsg));
        } else {
          pendingReq.resolve(response.result);
        }
        this.pendingRequests.delete(response.id);
      }
    } 
    // If it has 'event', it's a push event
    else if (msg && typeof msg.event === 'string') {
      if (msg.event === 'backend:ready') {
        if (this.handshakeStarted) return;
        this.handshakeStarted = true;
        const generation = String(msg.data?.generation || 'unknown');
        void this.rehydrateTrustedBackend(generation, child);
        return;
      }
      const eventMsg = msg as RPCEvent;
      // Broadcast to renderer
      const channel = eventMsg.event;
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
