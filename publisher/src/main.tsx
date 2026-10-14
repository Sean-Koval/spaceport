import { createRoot } from 'react-dom/client';
import { App } from './App';
import '@radix-ui/themes/styles.css';
import './styles.css';
createRoot(document.getElementById('root')!).render(<App />);
