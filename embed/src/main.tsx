import './polyfills'; // first: everything below may call a polyfilled API
import { installGuard } from '#guard'; // second: the runtime guard counts errors from here on
import './styles/app.css';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { PageStore } from './store';
import { bootstrapToken } from './transport';
import { App } from './ui/App';
import { installFocusVisible } from './ui/focusVisible';

installFocusVisible(); // page-global; needed even for the no-token error state below

const root = createRoot(document.getElementById('root')!);
const token = bootstrapToken();

if (!token) {
  root.render(<p className="t2v-center">Open this chat from the app it belongs to.</p>);
} else {
  const store = new PageStore(token);
  installGuard(store);
  store.start();
  root.render(
    <StrictMode>
      <App store={store} />
    </StrictMode>,
  );
}
