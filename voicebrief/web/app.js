/* VoiceBrief PWA — 녹음 → 업로드 → 진행 폴링 → 결과 표시
 *
 * 브라우저별 MediaRecorder 지원:
 *  - Chrome/Android: audio/webm;codecs=opus  (서버에서 ffmpeg으로 mp3 변환)
 *  - iOS Safari 14.3+: audio/mp4            (CLOVA 직접 지원)
 */
(() => {
  'use strict';

  const API = ''; // 같은 오리진에서 서빙. 분리 배포 시 'https://api.example.com' 로 변경
  const TOKEN = localStorage.getItem('vb_token') || ''; // API_TOKEN 사용 시 저장

  const $ = (id) => document.getElementById(id);
  const el = {
    timer: $('timer'), level: $('level'), recState: $('recState'),
    btnRecord: $('btnRecord'), btnRecordLabel: $('btnRecordLabel'),
    btnPause: $('btnPause'), btnStop: $('btnStop'),
    note: $('note'), fileInput: $('fileInput'),
    progressCard: $('progressCard'), progressTitle: $('progressTitle'),
    progressError: $('progressError'), steps: $('steps'),
    btnCloseProgress: $('btnCloseProgress'),
    historyList: $('historyList'), btnRefresh: $('btnRefresh'),
    healthLine: $('healthLine'),
    modal: $('modal'), modalContent: $('modalContent'), btnCloseModal: $('btnCloseModal'),
  };

  let mediaRecorder = null;
  let stream = null;
  let chunks = [];
  let startedAt = 0;
  let elapsedBeforePause = 0;
  let timerHandle = null;
  let pollHandle = null;
  let audioCtx = null, analyser = null, levelHandle = null;

  // ── 유틸 ────────────────────────────────────────────────────────────────
  const headers = () => (TOKEN ? { Authorization: `Bearer ${TOKEN}` } : {});

  function fmtTime(ms) {
    const t = Math.floor(ms / 1000);
    const m = String(Math.floor(t / 60)).padStart(2, '0');
    const s = String(t % 60).padStart(2, '0');
    return `${m}:${s}`;
  }

  function fmtDuration(ms) {
    if (!ms) return '';
    const t = Math.floor(ms / 1000);
    if (t < 60) return `${t}초`;
    return `${Math.floor(t / 60)}분 ${t % 60}초`;
  }

  function pickMimeType() {
    const candidates = [
      'audio/webm;codecs=opus',
      'audio/webm',
      'audio/mp4',            // iOS Safari
      'audio/aac',
      'audio/ogg;codecs=opus',
    ];
    for (const type of candidates) {
      if (window.MediaRecorder && MediaRecorder.isTypeSupported(type)) return type;
    }
    return '';
  }

  function extFor(mime) {
    if (!mime) return 'webm';
    if (mime.includes('mp4') || mime.includes('aac')) return 'm4a';
    if (mime.includes('ogg')) return 'ogg';
    return 'webm';
  }

  // ── 타이머 & 레벨 미터 ──────────────────────────────────────────────────
  function tick() {
    const ms = elapsedBeforePause + (startedAt ? Date.now() - startedAt : 0);
    el.timer.textContent = fmtTime(ms);
  }

  function startLevelMeter(srcStream) {
    try {
      audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      const source = audioCtx.createMediaStreamSource(srcStream);
      analyser = audioCtx.createAnalyser();
      analyser.fftSize = 512;
      source.connect(analyser);
      const data = new Uint8Array(analyser.frequencyBinCount);
      const draw = () => {
        analyser.getByteTimeDomainData(data);
        let peak = 0;
        for (const v of data) peak = Math.max(peak, Math.abs(v - 128));
        el.level.style.width = `${Math.min(100, (peak / 128) * 220)}%`;
        levelHandle = requestAnimationFrame(draw);
      };
      draw();
    } catch (e) { /* 레벨 미터는 부가 기능 — 실패해도 녹음은 계속 */ }
  }

  function stopLevelMeter() {
    if (levelHandle) cancelAnimationFrame(levelHandle);
    levelHandle = null;
    el.level.style.width = '0%';
    if (audioCtx) { audioCtx.close().catch(() => {}); audioCtx = null; }
  }

  // ── 녹음 제어 ───────────────────────────────────────────────────────────
  async function startRecording() {
    if (!navigator.mediaDevices?.getUserMedia) {
      alert('이 브라우저는 녹음을 지원하지 않습니다. HTTPS 또는 localhost에서 열어주세요.');
      return;
    }
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
    } catch (err) {
      alert(`마이크 권한이 필요합니다: ${err.name}\n브라우저 설정에서 마이크를 허용해주세요.`);
      return;
    }

    const mimeType = pickMimeType();
    chunks = [];
    mediaRecorder = new MediaRecorder(stream, mimeType ? { mimeType, audioBitsPerSecond: 64000 } : undefined);
    mediaRecorder.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data); };
    mediaRecorder.onstop = handleStop;
    mediaRecorder.start(1000); // 1초 단위로 청크 확보(중간에 탭이 죽어도 데이터 보존)

    elapsedBeforePause = 0;
    startedAt = Date.now();
    timerHandle = setInterval(tick, 200);
    startLevelMeter(stream);

    el.btnRecordLabel.textContent = '녹음 중';
    el.btnRecord.classList.add('recording');
    el.btnRecord.disabled = true;
    el.btnPause.disabled = false;
    el.btnStop.disabled = false;
    el.recState.textContent = '● 녹음 중';
    el.recState.classList.add('live');
  }

  function togglePause() {
    if (!mediaRecorder) return;
    if (mediaRecorder.state === 'recording') {
      mediaRecorder.pause();
      elapsedBeforePause += Date.now() - startedAt;
      startedAt = 0;
      el.btnPause.textContent = '이어서 녹음';
      el.recState.textContent = '‖ 일시정지';
      el.recState.classList.remove('live');
    } else if (mediaRecorder.state === 'paused') {
      mediaRecorder.resume();
      startedAt = Date.now();
      el.btnPause.textContent = '일시정지';
      el.recState.textContent = '● 녹음 중';
      el.recState.classList.add('live');
    }
  }

  function stopRecording() {
    if (!mediaRecorder || mediaRecorder.state === 'inactive') return;
    mediaRecorder.stop();
    clearInterval(timerHandle);
    stopLevelMeter();
    if (stream) stream.getTracks().forEach((t) => t.stop());
  }

  function resetRecorderUI() {
    el.timer.textContent = '00:00';
    el.btnRecordLabel.textContent = '녹음 시작';
    el.btnRecord.classList.remove('recording');
    el.btnRecord.disabled = false;
    el.btnPause.disabled = true;
    el.btnPause.textContent = '일시정지';
    el.btnStop.disabled = true;
    el.recState.textContent = '대기 중';
    el.recState.classList.remove('live');
    elapsedBeforePause = 0;
    startedAt = 0;
  }

  async function handleStop() {
    const mime = mediaRecorder?.mimeType || 'audio/webm';
    const blob = new Blob(chunks, { type: mime });
    resetRecorderUI();
    if (blob.size < 1000) {
      alert('녹음이 너무 짧습니다.');
      return;
    }
    const filename = `rec-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '')}.${extFor(mime)}`;
    await upload(blob, filename);
  }

  // ── 업로드 & 진행 폴링 ──────────────────────────────────────────────────
  const STEPS = ['upload', 'transcribing', 'summarizing', 'delivering', 'done'];

  function setStep(active, state = 'active') {
    const idx = STEPS.indexOf(active);
    el.steps.querySelectorAll('li').forEach((li, i) => {
      li.classList.remove('active', 'done', 'failed');
      if (i < idx) li.classList.add('done');
      else if (i === idx) li.classList.add(state === 'failed' ? 'failed' : 'active');
    });
    if (active === 'done') el.steps.querySelectorAll('li').forEach((li) => li.classList.add('done'));
  }

  function showProgress(title) {
    el.progressCard.classList.remove('hidden');
    el.progressTitle.textContent = title;
    el.progressError.classList.add('hidden');
    el.btnCloseProgress.classList.add('hidden');
    el.progressCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  async function upload(blob, filename) {
    showProgress('업로드 중…');
    setStep('upload');

    const form = new FormData();
    form.append('file', blob, filename);
    form.append('note', el.note.value || '');

    let res;
    try {
      res = await fetch(`${API}/api/recordings/process`, {
        method: 'POST', body: form, headers: headers(),
      });
    } catch (err) {
      return failProgress(`업로드 실패: ${err.message}`);
    }
    if (!res.ok) {
      const detail = await res.text();
      return failProgress(`업로드 실패 (${res.status}): ${detail.slice(0, 200)}`);
    }
    const { job_id } = await res.json();
    el.progressTitle.textContent = '처리 중…';
    pollJob(job_id);
    loadHistory();
  }

  function failProgress(message) {
    el.progressError.textContent = message;
    el.progressError.classList.remove('hidden');
    el.btnCloseProgress.classList.remove('hidden');
    el.progressTitle.textContent = '실패';
  }

  function pollJob(jobId) {
    clearInterval(pollHandle);
    let misses = 0;
    pollHandle = setInterval(async () => {
      try {
        const res = await fetch(`${API}/api/recordings/${jobId}`, { headers: headers() });
        if (!res.ok) throw new Error(`상태 조회 실패 ${res.status}`);
        const job = await res.json();
        misses = 0;

        if (job.status === 'failed') {
          clearInterval(pollHandle);
          setStep(STEPS.includes(job.status) ? job.status : 'upload', 'failed');
          failProgress(job.error || '처리에 실패했습니다.');
          loadHistory();
          return;
        }

        setStep(job.status === 'queued' ? 'upload' : job.status);
        el.progressTitle.textContent = job.status_label || '처리 중…';

        if (job.status === 'done') {
          clearInterval(pollHandle);
          el.btnCloseProgress.classList.remove('hidden');
          const failed = (job.deliveries || []).filter((d) => !d.ok);
          if (failed.length) {
            failProgress(`요약은 완료됐지만 일부 전송 실패: ${failed.map((d) => d.channel).join(', ')}`);
            el.progressTitle.textContent = '요약 완료 (전송 일부 실패)';
          }
          loadHistory();
          showDetail(job);
        }
      } catch (err) {
        if (++misses >= 5) {
          clearInterval(pollHandle);
          failProgress(`연결이 끊겼습니다: ${err.message}`);
        }
      }
    }, 2500);
  }

  // ── 기록 목록 ───────────────────────────────────────────────────────────
  const CATEGORY_STYLE = {
    meeting: ['회의', '#2563eb'], interview: ['면담', '#059669'],
    lecture: ['강연', '#7c3aed'], memo: ['메모', '#d97706'],
  };

  async function loadHistory() {
    try {
      const res = await fetch(`${API}/api/recordings?limit=30`, { headers: headers() });
      if (!res.ok) throw new Error(res.status);
      const { items } = await res.json();
      if (!items.length) {
        el.historyList.innerHTML = '<p class="empty">기록이 없습니다.</p>';
        return;
      }
      el.historyList.innerHTML = items.map((item) => {
        const [label, color] = CATEGORY_STYLE[item.category] || ['처리중', '#64748b'];
        const when = (item.created_at || '').replace('T', ' ').slice(5, 16);
        const status = item.status === 'done' ? ''
          : `<span class="chip-status ${item.status === 'failed' ? 'bad' : ''}">${item.status_label || ''}</span>`;
        return `
          <button class="item" data-id="${item.id}">
            <div class="item-top">
              <span class="chip" style="background:${color}">${label}</span>
              <span class="item-when">${when}${item.duration_ms ? ' · ' + fmtDuration(item.duration_ms) : ''}</span>
              ${status}
            </div>
            <div class="item-title">${escapeHtml(item.title || '(제목 없음)')}</div>
            <div class="item-desc">${escapeHtml(item.one_liner || item.error || '')}</div>
          </button>`;
      }).join('');
      el.historyList.querySelectorAll('.item').forEach((btn) => {
        btn.addEventListener('click', () => openDetail(btn.dataset.id));
      });
    } catch (err) {
      el.historyList.innerHTML = `<p class="empty">목록을 불러오지 못했습니다 (${err.message})</p>`;
    }
  }

  function escapeHtml(str) {
    return String(str).replace(/[&<>"']/g, (c) => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
  }

  async function openDetail(jobId) {
    const res = await fetch(`${API}/api/recordings/${jobId}`, { headers: headers() });
    if (!res.ok) return alert('상세를 불러오지 못했습니다.');
    showDetail(await res.json());
  }

  function showDetail(job) {
    const s = job.summary;
    if (!s) {
      el.modalContent.innerHTML = `<h2>${escapeHtml(job.filename)}</h2>
        <p>${escapeHtml(job.status_label || '')}</p>
        <p class="err">${escapeHtml(job.error || '')}</p>`;
    } else {
      const [label, color] = CATEGORY_STYLE[s.category] || ['기타', '#64748b'];
      const sections = (s.sections || []).map((sec) => `
        <h3>${escapeHtml(sec.heading)}</h3>
        <ul>${sec.bullets.map((b) => `<li>${escapeHtml(b)}</li>`).join('')}</ul>`).join('');
      const actions = (s.action_items || []).length ? `
        <h3>액션 아이템</h3>
        <ul>${s.action_items.map((a) => `<li>${escapeHtml(a.task)}
          <small>${escapeHtml(a.owner || '미지정')} · ${escapeHtml(a.due || '기한 미정')}</small></li>`).join('')}</ul>` : '';
      const delivery = (job.deliveries || []).map((d) =>
        `<span class="chip-status ${d.ok ? '' : 'bad'}">${d.channel} ${d.ok ? '✓' : '✕'}</span>`).join(' ');
      const retry = (job.deliveries || []).some((d) => !d.ok)
        ? `<button class="btn btn-ghost" id="btnResend" data-id="${job.id}">전송 재시도</button>` : '';

      el.modalContent.innerHTML = `
        <span class="chip" style="background:${color}">${label}</span>
        <h2>${escapeHtml(s.title)}</h2>
        <p class="one-liner">${escapeHtml(s.one_liner || '')}</p>
        ${(s.keywords || []).map((k) => `<span class="kw">#${escapeHtml(k)}</span>`).join('')}
        ${sections}${actions}
        <div class="delivery">${delivery} ${retry}</div>
        <a class="btn btn-ghost" href="${API}/api/recordings/${job.id}/markdown" target="_blank" rel="noopener">전체 보고서(MD)</a>`;

      const resendBtn = document.getElementById('btnResend');
      if (resendBtn) {
        resendBtn.addEventListener('click', async () => {
          resendBtn.disabled = true; resendBtn.textContent = '재전송 중…';
          const r = await fetch(`${API}/api/recordings/${job.id}/resend`, { method: 'POST', headers: headers() });
          resendBtn.textContent = r.ok ? '재전송 완료' : '재전송 실패';
          loadHistory();
        });
      }
    }
    el.modal.classList.remove('hidden');
  }

  async function loadHealth() {
    try {
      const res = await fetch(`${API}/api/health`);
      const { services } = await res.json();
      const names = { stt: 'STT', llm: 'LLM', mail: '메일', telegram: '텔레그램' };
      el.healthLine.textContent = Object.entries(names)
        .map(([k, n]) => `${n} ${services[k] ? '✓' : '✕'}`).join('  ·  ');
    } catch {
      el.healthLine.textContent = '서버에 연결할 수 없습니다.';
    }
  }

  // ── 이벤트 바인딩 ───────────────────────────────────────────────────────
  el.btnRecord.addEventListener('click', startRecording);
  el.btnPause.addEventListener('click', togglePause);
  el.btnStop.addEventListener('click', stopRecording);
  el.btnRefresh.addEventListener('click', loadHistory);
  el.btnCloseProgress.addEventListener('click', () => el.progressCard.classList.add('hidden'));
  el.btnCloseModal.addEventListener('click', () => el.modal.classList.add('hidden'));
  el.modal.addEventListener('click', (e) => { if (e.target === el.modal) el.modal.classList.add('hidden'); });
  el.fileInput.addEventListener('change', (e) => {
    const file = e.target.files[0];
    if (file) upload(file, file.name);
    e.target.value = '';
  });

  // 녹음 중 화면이 꺼지지 않게(지원 브라우저 한정)
  let wakeLock = null;
  document.addEventListener('visibilitychange', async () => {
    if (document.visibilityState === 'visible' && mediaRecorder?.state === 'recording' && 'wakeLock' in navigator) {
      try { wakeLock = await navigator.wakeLock.request('screen'); } catch {}
    }
  });

  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  }

  loadHealth();
  loadHistory();
})();
