import { store } from '../store';
import type { AppState } from '../store';
import { enqueueSpeech, stopSpeech } from './speech';
import { refreshModels, sendText } from './actions';
import type { BusEvent, TtsSpeakPayload } from './types';

export const SESSION_SCOPED_EVENTS = new Set([
  'assistant_start',
  'assistant_delta',
  'reasoning_delta',
  'assistant_done',
  'assistant_cancelled',
  'assistant_error',
  'tool_call',
  'tool_done',
  'tool_error',
  'session_busy',
  'session_update',
  'model_fallback',
]);

function eventSession(payload: Record<string, unknown> | undefined): string | null {
  if (!payload) return null;
  return (payload.session as string) || (payload.id as string) || null;
}

export function handleEvent(msg: BusEvent): void {
  const { kind, payload, t } = msg;
  if (kind === 'ping' || kind === 'hello') return;

  store.appendLog(
    `[${new Date(t * 1000).toLocaleTimeString()}] ${kind} ${JSON.stringify(payload || {}).slice(0, 200)}`,
  );

  if (store.state.sessionId && SESSION_SCOPED_EVENTS.has(kind)) {
    const target = eventSession(payload);
    if (target && target !== store.state.sessionId) return;
  }

  const p = payload || {};

  switch (kind) {
    case 'assistant_start': {
      if (!store.ttsTurnOpen) {
        store.ttsTurnOpen = true;
        store.ttsAggId = null;
        store.ttsAggCount = 0;
      }
      store.startAssistant();
      break;
    }
    case 'assistant_delta': {
      store.appendDelta(String(p.text ?? ''));
      break;
    }
    case 'reasoning_delta': {
      store.appendReasoning(String(p.text ?? ''));
      break;
    }
    case 'assistant_done': {
      store.doneAssistant();
      if (p.final) store.ttsTurnOpen = false;
      break;
    }
    case 'assistant_cancelled': {
      store.cancelAssistant();
      store.ttsTurnOpen = false;
      break;
    }
    case 'assistant_error': {
      store.addTool(`⚠ greška: ${p.error}`);
      store.ttsTurnOpen = false;
      break;
    }
    case 'session_busy': {
      store.set({ busy: !!p.busy, queued: Number(p.queued || 0) });
      break;
    }
    case 'tool_call': {
      store.addTool(`→ ${p.tool}(${JSON.stringify(p.args)})`);
      break;
    }
    case 'tool_done': {
      if (p.denied) store.addTool(`⛔ ${p.tool} odbijen`);
      break;
    }
    case 'tool_error': {
      store.addTool(`⚠ ${p.tool}: ${p.error}`);
      break;
    }
    case 'kilo_start': {
      store.addTool(`▶ kilo: ${String(p.prompt || '').slice(0, 80)}…`);
      break;
    }
    case 'kilo_done': {
      const elapsed =
        typeof p.elapsed === 'number' ? p.elapsed.toFixed(1) : String(p.elapsed ?? '');
      store.addTool(`■ kilo ok=${p.ok} exit=${p.exit} elapsed=${elapsed}s`);
      break;
    }
    case 'recording_start': {
      store.set({ recording: true });
      break;
    }
    case 'recording_end': {
      store.set({ recording: false });
      break;
    }
    case 'listen_enter': {
      store.set({ listenReasons: (p.reasons as string[]) || [] });
      break;
    }
    case 'listen_exit': {
      store.set({ listenReasons: [] });
      break;
    }
    case 'ptt_recording_start': {
      store.set({ recording: true });
      store.addTool('🎙 PTT snima…');
      break;
    }
    case 'ptt_recording_end': {
      store.set({ recording: false });
      break;
    }
    case 'whisper_loading': {
      store.addTool(`… učitavam Whisper (${p.model})`);
      break;
    }
    case 'whisper_ready': {
      store.addTool(`✓ Whisper spreman (${p.device})`);
      break;
    }
    case 'whisper_result': {
      store.addTool(`✓ STT (${p.language}): "${String(p.text || '').slice(0, 120)}"`);
      break;
    }
    case 'tts_speak': {
      enqueueSpeech(p as unknown as TtsSpeakPayload);
      break;
    }
    case 'tts_stop': {
      stopSpeech();
      break;
    }
    case 'tts_done': {
      store.ttsAggCount += 1;
      const text = `✓ TTS ×${store.ttsAggCount} (${p.engine})`;
      if (store.ttsAggId != null) store.updateTool(store.ttsAggId, text);
      else store.ttsAggId = store.addTool(text);
      break;
    }
    case 'tts_error': {
      store.addTool(`⚠ TTS: ${p.error}`);
      break;
    }
    case 'tts_fallback': {
      store.addTool(
        `⚠ TTS ${p.engine} nije uspeo (${String(p.error || '').slice(0, 80)}) — prelazim na say`,
      );
      break;
    }
    case 'llm_retry': {
      const attempt = Number(p.attempt ?? 0) + 1;
      store.addTool(`… LLM pokušaj ${attempt} posle ${p.reason || 'greške'} (čekam ${p.delay}s)`);
      break;
    }
    case 'bus_overflow': {
      store.addTool('⚠ red događaja prepun — najstariji događaji odbačeni');
      break;
    }
    case 'local_model_loading': {
      store.addTool(`… učitavam lokalni model: ${p.id}`);
      break;
    }
    case 'local_model_ready': {
      store.addTool(`✓ lokalni model učitan: ${p.loaded_id}`);
      void refreshModels();
      break;
    }
    case 'local_model_unloaded': {
      store.addTool('■ lokalni model oslobođen iz RAM-a');
      void refreshModels();
      break;
    }
    case 'local_model_error': {
      store.addTool(`⚠ lokalni model: ${p.error || 'load failed'}`);
      break;
    }
    case 'model_fallback': {
      const name = String(p.model || '').replace(/^local:/, '');
      store.addTool(
        `⚠ lokalni model ${name} nije dostupan (${p.reason || 'greška'}) — odgovaram cloud modelom`,
      );
      break;
    }
    case 'voice_ptt_transcribed': {
      if (!p.ok) {
        store.addTool(`⚠ PTT: ${p.error || 'transcribe failed'}`);
        break;
      }
      if (p.skipped === 'empty') {
        store.addTool('… PTT: ništa nisam čuo');
        break;
      }
      const text = String(p.text || '');
      if (!text) break;
      if (p.auto_send) {
        void sendText(text, { interrupt: true, userLabel: '🎙 ' + text });
      } else {
        const draft = store.state.draft;
        store.set({ draft: (draft ? draft + ' ' : '') + text });
        store.addTool('🎙 PTT transkript je u input polju — pritisni Pošalji.');
      }
      break;
    }
    case 'permission_request': {
      store.set({
        pendingPerm: {
          request_id: String(p.request_id || ''),
          tool: String(p.tool || ''),
          args: (p.args as Record<string, unknown>) || {},
          reason: p.reason as string | undefined,
        },
      });
      break;
    }
    case 'permissions_changed': {
      store.set({ permissions: p as unknown as AppState['permissions'] });
      break;
    }
    case 'ptt_status': {
      store.set({ ptt: p as unknown as AppState['ptt'] });
      break;
    }
    case 'local_model_pulling': {
      const pulls = (store.state.localPulls || []).filter((q) => q.tag !== p.tag);
      pulls.push({
        tag: String(p.tag),
        status: String(p.status),
        percent: Number(p.percent || 0),
        detail: (p.detail as string) || '',
      });
      store.set({ localPulls: pulls });
      break;
    }
    default:
      break;
  }
}
