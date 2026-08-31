import React from 'react';
import ReactDOM from 'react-dom/client';
import { HashRouter } from 'react-router-dom';
import App from './App';
import './styles/globals.css';

console.log('--- REACT APP STARTING ---');
console.log('window.electronAPI:', window.electronAPI);
if (!window.electronAPI) {
  alert('FATAL: window.electronAPI is undefined!\n' + ((window as any).preloadError || 'Unknown Error'));
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <HashRouter>
      <App />
    </HashRouter>
  </React.StrictMode>
);
