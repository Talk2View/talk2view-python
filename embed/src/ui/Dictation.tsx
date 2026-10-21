/**
 * The voice button (TS chat/composer-extras.tsx): one button for the whole dictation, the
 * brand wave face. Rest: Smoke bars on Teal. Listening: inverted, the bars move like a
 * level meter, a tap stops. Transcribing: the bars settle into short dashes that pulse
 * left to right, a ring circles the face, taps are ignored. The glyph never swaps and the
 * button never remounts, so nothing flickers. The animations say what the mic is doing,
 * so they run under reduced motion too.
 */
import { useEffect, useRef, type FC } from 'react';
import { resultOf } from '../store';
import { useModel, useStore } from './context';

/* The Talk2View voice mark: four SHARP bars at 0.42 / 1.0 / 0.66 / 0.30 of the height,
   each 44% of its slot wide, in a 1.1 : 1 box, growing from the middle out. */
const WAVE_LEVELS = [0.42, 1.0, 0.66, 0.3];
const BUSY_LEVELS = [0.5, 0.5, 0.5, 0.5];

export const BrandWaveform: FC<{ mode?: 'rest' | 'live' | 'busy' }> = ({ mode = 'rest' }) => (
  <svg viewBox="0 0 110 100" fill="currentColor" aria-hidden="true" className="t2v-wave" width={12} height={11} overflow="visible">
    {(mode === 'busy' ? BUSY_LEVELS : WAVE_LEVELS).map((level, i) => (
      <rect
        key={i}
        x={27.5 * (i + 0.5) - 6.05}
        y={50 - level * 50}
        width={12.1}
        height={level * 100}
        className={mode === 'rest' ? undefined : 't2v-wave-bar t2v-wave-' + mode}
        // Listening: out of phase, like speech. Busy: in order, left to right.
        style={mode === 'rest' ? undefined : { animationDelay: String(mode === 'live' ? i * -0.23 : i * 0.14) + 's' }}
      />
    ))}
  </svg>
);

/** The beam that circles the face while transcribing: chat.src.css draws it with
 * conic-gradient + mask + @property, none of which Chromium 83 has; an SVG ring with a
 * dashed stroke that rotates is the same picture. */
const Ring: FC = () => (
  <svg className="t2v-voice-ring" viewBox="0 0 36 36" aria-hidden="true">
    <circle cx="18" cy="18" r="17" fill="none" strokeWidth="2" strokeDasharray="38 69" strokeLinecap="butt" />
  </svg>
);

export const ComposerDictation: FC = () => {
  const store = useStore();
  const model = useModel();
  const { state } = model;
  const dictation = state.dictation;
  // Absorbs a second tap arriving before the first dictationStart's own result, or the
  // slice's phase, has landed: without this, a quick double tap
  // sends a second dictationStart the server refuses `busy`, which failureText's own
  // command-type branch now also softens — but the fix belongs here, closer to how TS's
  // own composer (assistant-ui) absorbs a tap on a command already in flight. Python's
  // busy check is synchronous, before any capture call, so this never needs a WATCH_MS-
  // style fallback the way a slower command elsewhere does (Settings.tsx's usePending):
  // the slice leaves "idle" or the command's own result lands well within one round trip.
  const startingRef = useRef<string | null>(null);
  const idle = dictation?.phase === 'idle';
  useEffect(() => {
    if (startingRef.current !== null && (!idle || resultOf(model, startingRef.current) !== null)) {
      startingRef.current = null;
    }
  }, [idle, model]);
  if (!state.capabilities.audio || !state.options.features.dictation || !dictation) return null;
  if (dictation.phase === 'idle') {
    return (
      <button
        type="button"
        className="t2v-send aui-composer-dictate"
        aria-label="Start voice input"
        title="Voice input"
        onClick={() => {
          if (startingRef.current !== null) return; // a start is already in flight
          startingRef.current = store.command({ type: 'dictationStart' });
        }}
      >
        <BrandWaveform />
      </button>
    );
  }
  const busy = dictation.phase === 'transcribing';
  return (
    <button
      type="button"
      className="t2v-send aui-composer-stop-dictation t2v-voice-face"
      aria-label={busy ? 'Transcribing' : 'Stop voice input'}
      title={busy ? 'Transcribing…' : 'Stop dictation'}
      aria-busy={busy || undefined}
      data-busy={busy ? '' : undefined}
      onClick={() => {
        if (!busy) store.command({ type: 'dictationStop' });
      }}
    >
      <BrandWaveform mode={busy ? 'busy' : 'live'} />
      {busy ? <Ring /> : null}
    </button>
  );
};
