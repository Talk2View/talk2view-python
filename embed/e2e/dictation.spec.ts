import { expect, test } from './fixtures';

async function signIn(page: import('@playwright/test').Page): Promise<void> {
  await page.getByRole('button', { name: 'Account' }).click();
  await page.getByLabel('Email').fill('ana@example.com');
  await page.getByLabel('Password').fill('pw');
  // exact: true — the Account view also carries a "Sign in" mode-toggle link, the
  // established pattern elsewhere (settings.spec.ts's own signIn()).
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Account' }).locator('.t2v-dot')).toBeVisible();
}

test.describe('voice settings (account mode with a host microphone)', () => {
  test.use({ backendArgs: ['--mode', 'account', '--audio'] });

  test('Voice model and Spoken language follow the TS rules', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signIn(page);
    await page.getByRole('button', { name: 'Settings' }).click();
    const voice = page.getByLabel('Voice model');
    await expect(voice).toBeEnabled();
    await expect(voice.locator('option')).toHaveText(['Default', 'Scribe v2', 'faster-whisper-base']);
    const language = page.getByLabel('Spoken language');
    await expect(language.locator('option').first()).toHaveText('Detect automatically');
    await expect(language.locator('option')).toHaveCount(9); // auto + 8 common (faster-whisper-base is unrestricted)
    await expect(page.getByText('A hint for the voice model.')).toBeVisible();
    await voice.selectOption('scribe_v2');
    await expect(language.locator('option')).toHaveText(['Detect automatically', 'English', 'Vietnamese']);
    await expect(page.getByText('The 2 languages Scribe v2 can transcribe.')).toBeVisible();
    await language.selectOption('vi');
    await expect(language).toHaveValue('vi');
    // Back to the unrestricted model: Vietnamese is among the common eight, so it stays.
    await voice.selectOption('');
    await expect(language).toHaveValue('vi');
    // A model that cannot transcribe the chosen language sends it back to auto-detect —
    // checked the moment the Voice model select's own onChange fires, against whatever
    // audioModels the page already has; re-selecting an UNCHANGED value fires no onChange
    // at all, so this control's own SSE update (through backend.py's stdin, not a direct
    // page write) has to actually land before the switch, or the check runs on the old
    // list and never resets anything. `toPass` retries the whole switch, not just the
    // read, since a settled page state is what proves the update has arrived, not a timer.
    backend.control('audio-models-english-only');
    await expect(async () => {
      await voice.selectOption('');
      await voice.selectOption('scribe_v2');
      await expect(language).toHaveValue('');
    }).toPass({ timeout: 15000 });
    await expect(page.getByText('Scribe v2 transcribes English only.')).toBeVisible();
  });

  test('the audio catalogue: a real empty answer is enabled, a failed read says so', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signIn(page);
    await page.getByRole('button', { name: 'Settings' }).click();
    const voice = page.getByLabel('Voice model');
    await expect(voice).toBeEnabled(); // the normal, already-loaded list

    // A partner with nothing configured: "ready", so the picker enables with only
    // Default — never read the same as "still loading".
    backend.control('audio-models-empty');
    await expect(voice.locator('option')).toHaveText(['Default']);
    await expect(voice).toBeEnabled();
    await expect(page.getByText('Transcribes what you say when you use the mic.')).toBeVisible();

    // A failed read: disabled, same as loading, but it says why instead of "Loading…".
    backend.control('audio-models-error');
    await expect(voice).toBeDisabled();
    await expect(page.getByText('Couldn’t load the list — the default will be used.')).toBeVisible();
  });
});

test.describe('the mic button, transcribing observed (account mode with a host microphone)', () => {
  // A local FakeCapture + this same-process fake engine answer POST /v1/audio/
  // transcriptions near-instantly, so without a deliberate delay "transcribing" can come
  // and go inside one Playwright poll (measured: "element(s) not found" on the busy
  // locator once the machine this ran on was no longer under the load that had been
  // masking it). Scoped to its own describe block: the other two mic specs below only
  // check state once everything has settled, so they need no delay.
  test.use({ backendArgs: ['--mode', 'account', '--audio', '--slow-transcribe', '0.3'] });

  test('rest, listening, transcribing, then the transcript lands in the composer', async ({ page, backend }) => {
    await page.goto(backend.url);
    const mic = page.getByRole('button', { name: 'Start voice input' });
    await expect(mic).toBeVisible();
    await expect(mic).toHaveClass(/aui-composer-dictate/);
    const box = (await mic.boundingBox())!;
    expect([box.width, box.height]).toEqual([28, 28]);
    // The rest mic is a quiet ghost, transparent and muted — the send
    // button is the one Teal circle in the row.
    expect(await page.evaluate(() => getComputedStyle(document.querySelector('.aui-composer-dictate')!).backgroundColor)).toBe('rgba(0, 0, 0, 0)');
    expect(await page.evaluate(() => getComputedStyle(document.querySelector('.aui-composer-dictate')!).color)).toBe('rgb(103, 119, 124)');
    await expect(mic.locator('svg rect')).toHaveCount(4);
    await mic.click();
    const stop = page.getByRole('button', { name: 'Stop voice input' });
    await expect(stop).toBeVisible();
    expect(await page.evaluate(() => getComputedStyle(document.querySelector('.aui-composer-stop-dictation')!).backgroundColor)).toBe('rgb(2, 28, 37)');
    expect(await page.evaluate(() => getComputedStyle(document.querySelector('.t2v-wave-live')!).animationName)).toBe('t2v-wave-live');
    // Signed out with guests allowed: the mic press started the guest while talking.
    await expect(page.getByRole('button', { name: 'Account' })).toBeVisible();
    await stop.click();
    const busy = page.locator('.aui-composer-stop-dictation[data-busy]');
    await expect(busy).toHaveAttribute('aria-label', 'Transcribing');
    await expect(busy.locator('.t2v-voice-ring')).toBeVisible();
    await expect(page.getByRole('textbox', { name: 'Message' })).toHaveValue('hello from the mic');
    await expect(page.getByRole('button', { name: 'Start voice input' })).toBeVisible();
    await expect.poll(() => backend.transcriptions.map((t) => t.model)).toEqual(['faster-whisper-base']);
    // A second dictation appends after a space.
    await page.getByRole('button', { name: 'Start voice input' }).click();
    await page.getByRole('button', { name: 'Stop voice input' }).click();
    await expect(page.getByRole('textbox', { name: 'Message' })).toHaveValue('hello from the mic hello from the mic');
  });

  test('a reload does not re-insert the last transcript into a fresh composer', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Start voice input' }).click();
    await page.getByRole('button', { name: 'Stop voice input' }).click();
    await expect(page.getByRole('textbox', { name: 'Message' })).toHaveValue('hello from the mic');
    // The slice keeps that transcript until the next start() — the page reconnecting must
    // not read it as new: a Slicer host re-opening the panel would
    // otherwise dictate the last clip's text into an empty composer all over again.
    await page.reload();
    await expect(page.getByRole('button', { name: 'Start voice input' })).toBeVisible();
    await expect(page.getByRole('textbox', { name: 'Message' })).toHaveValue('');
    await expect(page.locator('.t2v-notice')).toHaveCount(0);
  });
});

test.describe('the mic button (account mode with a host microphone)', () => {
  test.use({ backendArgs: ['--mode', 'account', '--audio'] });

  test('a refused transcription is a notice, and the mic comes back', async ({ page, backend }) => {
    await page.goto(backend.url);
    backend.control('transcription-refuse');
    await page.getByRole('button', { name: 'Start voice input' }).click();
    await page.getByRole('button', { name: 'Stop voice input' }).click();
    await expect(page.locator('.t2v-notice')).toContainText('Model faster-whisper-base is not enabled for this partner');
    await expect(page.getByRole('button', { name: 'Start voice input' })).toBeVisible();
  });

  test('the animations keep running under reduced motion: they say what the mic is doing', async ({ page, backend }) => {
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Start voice input' }).click();
    // Wait for the listening state itself, not just the click: dictationStart is a real
    // round trip (the server's own "listening" slice update over SSE), and reading
    // .t2v-wave-live before it lands hits a null querySelector (measured: "parameter 1 is
    // not of type 'Element'", the click's own promise resolving well before the render).
    await expect(page.getByRole('button', { name: 'Stop voice input' })).toBeVisible();
    expect(await page.evaluate(() => getComputedStyle(document.querySelector('.t2v-wave-live')!).animationName)).toBe('t2v-wave-live');
  });
});

test.describe('a start still opening the mic (account mode with a host microphone)', () => {
  // --slow-capture-start holds the FIRST dictationStart's own FakeCapture.start() open
  // well past two Playwright clicks, so a second tap deterministically lands before the
  // slice leaves "idle" or the first command's result is known — unlike racing a plain
  // click against however fast the SSE round trip happens to be on this machine.
  test.use({ backendArgs: ['--mode', 'account', '--audio', '--slow-capture-start', '2'] });

  test('two taps start one recording, not a "still working" notice', async ({ page, backend }) => {
    await page.goto(backend.url);
    let dictationStarts = 0;
    await page.route('**/command', async (route) => {
      const body = route.request().postDataJSON() as { type?: string } | null;
      if (body?.type === 'dictationStart') dictationStarts++;
      await route.continue();
    });
    const mic = page.getByRole('button', { name: 'Start voice input' });
    await mic.click();
    await mic.click(); // absorbed: the first dictationStart is still opening the (slow) mic
    await expect(page.locator('.t2v-notice')).toHaveCount(0);
    await expect(mic).toBeVisible(); // still idle: capture.start() has not returned yet
    await expect(page.getByRole('button', { name: 'Stop voice input' })).toBeVisible({ timeout: 5000 });
    expect(dictationStarts).toBe(1); // the second tap never reached the server at all
    await page.getByRole('button', { name: 'Stop voice input' }).click();
    await expect(page.getByRole('textbox', { name: 'Message' })).toHaveValue('hello from the mic');
    await expect.poll(() => backend.transcriptions.length).toBe(1);
  });
});

test.describe('a clip past the cap (account mode with a host microphone)', () => {
  // --max-clip-seconds stands in for the real ten-minute MAX_CLIP_SECONDS, so the
  // auto-stop (and its own notice) can be observed without an actual ten-minute clip.
  test.use({ backendArgs: ['--mode', 'account', '--audio', '--max-clip-seconds', '0.2'] });

  test('the clip auto-stops and shows its own notice, transcript included', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Start voice input' }).click();
    // No stop click at all: the clip auto-stops on its own once --max-clip-seconds
    // elapses, however quickly that happens to land relative to "listening" rendering.
    await expect(page.locator('.t2v-notice')).toContainText('Stopped at 10 minutes.');
    await expect(page.getByRole('textbox', { name: 'Message' })).toHaveValue('hello from the mic');
    await expect(page.getByRole('button', { name: 'Start voice input' })).toBeVisible();
    await expect.poll(() => backend.transcriptions.length).toBe(1);
  });
});

test.describe('a capture that cannot start (account mode with a host microphone)', () => {
  test.use({ backendArgs: ['--mode', 'account', '--audio', '--capture-fails', 'no_microphone'] });

  test('a failing capture is a notice, and the mic comes back', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Start voice input' }).click();
    await expect(page.locator('.t2v-notice')).toContainText('No microphone was found.');
    await expect(page.getByRole('button', { name: 'Start voice input' })).toBeVisible();
  });
});

test.describe('no host microphone', () => {
  test.use({ backendArgs: ['--mode', 'account'] });
  test('no mic button at all', async ({ page, backend }) => {
    await page.goto(backend.url);
    await expect(page.getByRole('button', { name: 'Send' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Start voice input' })).toHaveCount(0);
  });
});
