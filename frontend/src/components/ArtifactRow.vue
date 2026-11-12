<script setup lang="ts">
import type { TaskArtifact } from '../api/types'

defineProps<{ artifact: TaskArtifact; label: string; icon: string; tone: string; description: string; previewLabel: string; busy: boolean; versions?: number; historyOpen?: boolean; historical?: boolean; sequence?: number }>()
defineEmits<{ (event: 'preview', item: TaskArtifact): void; (event: 'open', item: TaskArtifact, reveal: boolean): void; (event: 'info', item: TaskArtifact): void; (event: 'history'): void }>()
</script>

<template>
  <div class="artifact-row" :class="{ 'artifact-row--unavailable': artifact.file_available === false, 'artifact-row--historical': historical, 'artifact-row--sequenced': sequence !== undefined }" :data-artifact-id="artifact.id">
    <div class="artifact-row-name">
      <span v-if="sequence !== undefined" class="artifact-sequence" role="img" :aria-label="`序号 ${sequence}`">{{ String(sequence).padStart(2, '0') }}</span>
      <span class="artifact-type-icon" :class="`artifact-type-icon--${tone}`" aria-hidden="true">{{ icon }}</span>
      <div class="artifact-row-copy"><strong>{{ label }} <span class="artifact-row-tag">{{ artifact.file_available === false ? '文件不可用' : description }}</span></strong><small :title="artifact.name">{{ artifact.name }}</small></div>
    </div>
    <div class="artifact-row-actions">
      <button v-if="previewLabel" class="button button-secondary button-small artifact-primary" type="button" :disabled="artifact.file_available === false" @click="$emit('preview', artifact)">{{ previewLabel }}</button>
      <button class="text-button" type="button" :disabled="artifact.file_available === false || busy" @click="$emit('open', artifact, false)">打开文件</button>
      <button class="text-button" type="button" :disabled="artifact.file_available === false || busy" @click="$emit('open', artifact, true)">目录</button>
      <button class="text-button" type="button" @click="$emit('info', artifact)">详情</button>
      <button v-if="versions && versions > 1" class="text-button" type="button" :aria-expanded="historyOpen" @click="$emit('history')">{{ historyOpen ? '收起' : '历史版本' }} · {{ versions }}</button>
    </div>
  </div>
</template>
