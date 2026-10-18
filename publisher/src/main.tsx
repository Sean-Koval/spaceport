import { createRoot } from 'react-dom/client';
import { App } from './App';
import '@radix-ui/themes/styles.css';
import './generated/airframe/airframe.css';
import './generated/airframe/airframe-components.css';
import './airframe-fonts.css';
import './styles.css';
createRoot(document.getElementById('root')!).render(<App />);
