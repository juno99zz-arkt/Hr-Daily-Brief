/**
 * VoiceBrief — React Native (Expo) 클라이언트 참고 구현
 *
 * PWA 대신 네이티브 앱으로 쓰고 싶을 때 사용하세요.
 * 백엔드 API 계약은 web/ PWA와 완전히 동일합니다.
 *
 * 준비:
 *   npx create-expo-app voicebrief-mobile
 *   npx expo install expo-av expo-file-system
 *   # app.json 에 마이크 권한 추가:
 *   #   ios.infoPlist.NSMicrophoneUsageDescription = "회의 녹음을 위해 마이크를 사용합니다"
 *   #   android.permissions = ["RECORD_AUDIO"]
 *
 * 이 파일을 App.tsx 로 복사하고 API_BASE 를 서버 주소로 바꾸면 동작합니다.
 */

import { Audio } from 'expo-av';
import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  ActivityIndicator, FlatList, SafeAreaView, ScrollView,
  StyleSheet, Text, TouchableOpacity, View,
} from 'react-native';

const API_BASE = 'http://192.168.0.10:8000'; // ← 서버 주소로 변경 (실기기는 LAN IP)
const API_TOKEN = '';                         // API_TOKEN 설정 시 입력

type JobStatus = 'queued' | 'transcribing' | 'summarizing' | 'delivering' | 'done' | 'failed';

const STEPS: { key: JobStatus; label: string }[] = [
  { key: 'queued', label: '오디오 업로드' },
  { key: 'transcribing', label: '텍스트 변환 (CLOVA)' },
  { key: 'summarizing', label: 'AI 요약' },
  { key: 'delivering', label: '메일 · 텔레그램 전송' },
  { key: 'done', label: '전송 완료' },
];

const authHeaders = () => (API_TOKEN ? { Authorization: `Bearer ${API_TOKEN}` } : {});

export default function App() {
  const [recording, setRecording] = useState<Audio.Recording | null>(null);
  const [isPaused, setIsPaused] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [job, setJob] = useState<any>(null);
  const [history, setHistory] = useState<any[]>([]);
  const [busy, setBusy] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // ── 기록 목록 ────────────────────────────────────────────────────────────
  const loadHistory = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/api/recordings?limit=30`, { headers: authHeaders() });
      const data = await res.json();
      setHistory(data.items || []);
    } catch { /* 오프라인 무시 */ }
  }, []);

  useEffect(() => {
    loadHistory();
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [loadHistory]);

  // ── 녹음 시작 ────────────────────────────────────────────────────────────
  const startRecording = async () => {
    const perm = await Audio.requestPermissionsAsync();
    if (!perm.granted) return alert('마이크 권한이 필요합니다.');

    await Audio.setAudioModeAsync({
      allowsRecordingIOS: true,
      playsInSilentModeIOS: true,
      staysActiveInBackground: true, // 화면이 꺼져도 녹음 유지
    });

    // HIGH_QUALITY 프리셋은 iOS/Android 모두 m4a(AAC)로 저장 → CLOVA 직접 지원
    const { recording: rec } = await Audio.Recording.createAsync(
      Audio.RecordingOptionsPresets.HIGH_QUALITY,
    );
    setRecording(rec);
    setIsPaused(false);
    setElapsed(0);
    timerRef.current = setInterval(() => setElapsed((v) => v + 1), 1000);
  };

  const togglePause = async () => {
    if (!recording) return;
    if (isPaused) {
      await recording.startAsync();
      timerRef.current = setInterval(() => setElapsed((v) => v + 1), 1000);
    } else {
      await recording.pauseAsync();
      if (timerRef.current) clearInterval(timerRef.current);
    }
    setIsPaused(!isPaused);
  };

  // ── 종료 → 업로드 ────────────────────────────────────────────────────────
  const stopAndUpload = async () => {
    if (!recording) return;
    if (timerRef.current) clearInterval(timerRef.current);

    await recording.stopAndUnloadAsync();
    const uri = recording.getURI();
    setRecording(null);
    setIsPaused(false);
    if (!uri) return;

    setBusy(true);
    try {
      const form = new FormData();
      // React Native의 FormData는 {uri, name, type} 객체를 받는다
      form.append('file', {
        uri,
        name: `rec-${Date.now()}.m4a`,
        type: 'audio/m4a',
      } as any);
      form.append('note', '');

      const res = await fetch(`${API_BASE}/api/recordings/process`, {
        method: 'POST',
        headers: authHeaders(), // Content-Type은 지정하지 않는다 (boundary 자동 설정)
        body: form,
      });
      if (!res.ok) throw new Error(`업로드 실패 ${res.status}`);
      const { job_id } = await res.json();
      setJob({ status: 'queued', status_label: '업로드 완료 · 대기 중' });
      pollJob(job_id);
      loadHistory();
    } catch (e: any) {
      alert(e.message);
    } finally {
      setBusy(false);
    }
  };

  const pollJob = (jobId: string) => {
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      try {
        const res = await fetch(`${API_BASE}/api/recordings/${jobId}`, { headers: authHeaders() });
        const data = await res.json();
        setJob(data);
        if (data.status === 'done' || data.status === 'failed') {
          if (pollRef.current) clearInterval(pollRef.current);
          loadHistory();
        }
      } catch { /* 재시도 */ }
    }, 2500);
  };

  const fmt = (sec: number) =>
    `${String(Math.floor(sec / 60)).padStart(2, '0')}:${String(sec % 60).padStart(2, '0')}`;

  const stepIndex = job ? STEPS.findIndex((s) => s.key === job.status) : -1;

  return (
    <SafeAreaView style={styles.container}>
      <ScrollView contentContainerStyle={styles.scroll}>
        <Text style={styles.title}>VoiceBrief</Text>

        <View style={styles.card}>
          <Text style={styles.timer}>{fmt(elapsed)}</Text>
          <Text style={styles.state}>
            {recording ? (isPaused ? '‖ 일시정지' : '● 녹음 중') : '대기 중'}
          </Text>

          <View style={styles.row}>
            {!recording ? (
              <TouchableOpacity style={[styles.btn, styles.btnRec]} onPress={startRecording}>
                <Text style={styles.btnText}>녹음 시작</Text>
              </TouchableOpacity>
            ) : (
              <>
                <TouchableOpacity style={[styles.btn, styles.btnGhost]} onPress={togglePause}>
                  <Text style={styles.btnText}>{isPaused ? '이어서' : '일시정지'}</Text>
                </TouchableOpacity>
                <TouchableOpacity style={[styles.btn, styles.btnStop]} onPress={stopAndUpload}>
                  <Text style={styles.btnText}>완료 · 전송</Text>
                </TouchableOpacity>
              </>
            )}
          </View>
          {busy && <ActivityIndicator style={{ marginTop: 12 }} color="#3b82f6" />}
        </View>

        {job && (
          <View style={styles.card}>
            <Text style={styles.cardTitle}>{job.status_label || '처리 중…'}</Text>
            {STEPS.map((step, i) => (
              <Text
                key={step.key}
                style={[
                  styles.step,
                  i < stepIndex && styles.stepDone,
                  i === stepIndex && styles.stepActive,
                ]}
              >
                {i < stepIndex ? '✓' : i === stepIndex ? '▶' : '·'}  {step.label}
              </Text>
            ))}
            {job.error && <Text style={styles.error}>{job.error}</Text>}
            {job.summary && (
              <View style={{ marginTop: 12 }}>
                <Text style={styles.summaryTitle}>{job.summary.title}</Text>
                <Text style={styles.summaryLine}>{job.summary.one_liner}</Text>
              </View>
            )}
          </View>
        )}

        <View style={styles.card}>
          <Text style={styles.cardTitle}>최근 요약</Text>
          <FlatList
            scrollEnabled={false}
            data={history}
            keyExtractor={(item) => item.id}
            ListEmptyComponent={<Text style={styles.state}>기록이 없습니다.</Text>}
            renderItem={({ item }) => (
              <View style={styles.item}>
                <Text style={styles.itemMeta}>
                  {item.category_label || item.status_label} · {(item.created_at || '').slice(5, 16)}
                </Text>
                <Text style={styles.summaryTitle}>{item.title}</Text>
                <Text style={styles.summaryLine} numberOfLines={2}>
                  {item.one_liner || item.error || ''}
                </Text>
              </View>
            )}
          />
        </View>
      </ScrollView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: '#0f172a' },
  scroll: { padding: 16 },
  title: { color: '#f1f5f9', fontSize: 24, fontWeight: '700', textAlign: 'center', marginBottom: 16 },
  card: { backgroundColor: '#1e293b', borderRadius: 14, padding: 18, marginBottom: 14 },
  cardTitle: { color: '#f1f5f9', fontSize: 16, fontWeight: '600', marginBottom: 10 },
  timer: { color: '#f1f5f9', fontSize: 44, fontWeight: '200', textAlign: 'center' },
  state: { color: '#94a3b8', fontSize: 13, textAlign: 'center', marginBottom: 14 },
  row: { flexDirection: 'row', justifyContent: 'center', gap: 8 },
  btn: { paddingVertical: 12, paddingHorizontal: 22, borderRadius: 999 },
  btnRec: { backgroundColor: '#ef4444' },
  btnStop: { backgroundColor: '#3b82f6' },
  btnGhost: { borderWidth: 1, borderColor: '#334155' },
  btnText: { color: '#fff', fontWeight: '600', fontSize: 15 },
  step: { color: '#64748b', fontSize: 14, paddingVertical: 5 },
  stepActive: { color: '#f1f5f9', fontWeight: '700' },
  stepDone: { color: '#22c55e' },
  error: { color: '#fca5a5', fontSize: 13, marginTop: 10 },
  summaryTitle: { color: '#f1f5f9', fontSize: 15, fontWeight: '600' },
  summaryLine: { color: '#94a3b8', fontSize: 13, marginTop: 2 },
  item: { borderTopWidth: 1, borderTopColor: '#334155', paddingVertical: 10 },
  itemMeta: { color: '#64748b', fontSize: 11, marginBottom: 3 },
});
