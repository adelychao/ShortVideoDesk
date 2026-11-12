<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { platformLogoUrl } from '../platformLogos'

const props = defineProps<{ platformId: string; name: string }>()
const failed = ref(false)
const src = computed(() => platformLogoUrl(props.platformId))
watch(src, () => { failed.value = false })
</script>

<template>
  <span class="platform-logo" aria-hidden="true">
    <img v-if="src && !failed" :src="src" alt="" @error="failed = true" />
    <span v-else class="platform-logo__fallback">{{ (name || platformId).slice(0, 1).toUpperCase() }}</span>
  </span>
</template>

<style scoped>
.platform-logo { display: inline-grid; place-items: center; flex: 0 0 auto; width: 28px; height: 28px; overflow: hidden; border: 1px solid #e7ebf0; border-radius: 8px; background: #fff; }
.platform-logo img { display: block; width: 78%; height: 78%; object-fit: contain; }
.platform-logo__fallback { display: grid; place-items: center; width: 100%; height: 100%; background: #eef2f7; color: #52657b; font-size: 12px; font-weight: 700; }
</style>
