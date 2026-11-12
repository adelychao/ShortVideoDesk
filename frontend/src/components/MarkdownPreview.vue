<script setup lang="ts">
import { computed } from 'vue'
import MarkdownIt from 'markdown-it'

const props = defineProps<{ content: string; managedImagePrefix?: string }>()
const markdown = new MarkdownIt({ html: false, linkify: false, typographer: false, breaks: true })
const defaultImageRenderer = markdown.renderer.rules.image
if (props.managedImagePrefix) markdown.enable('image')
else markdown.disable('image')
markdown.renderer.rules.image = (tokens, index, options, env, renderer) => {
  const source = String(tokens[index].attrGet('src') || '')
  const prefix = props.managedImagePrefix
  if (!prefix || !source.startsWith(prefix)) return ''
  const remainder = source.slice(prefix.length)
  if (!/^[0-9a-f]{32}\.(?:jpe?g|png|webp)(?:\?slot=[\w.-]+)?$/.test(remainder)) return ''
  return defaultImageRenderer?.(tokens, index, options, env, renderer) ?? ''
}
const defaultLinkRenderer = markdown.renderer.rules.link_open ?? ((tokens, index, options, _env, renderer) => renderer.renderToken(tokens, index, options))
markdown.renderer.rules.link_open = (tokens, index, options, env, renderer) => {
  tokens[index].attrSet('target', '_blank')
  tokens[index].attrSet('rel', 'noopener noreferrer')
  return defaultLinkRenderer(tokens, index, options, env, renderer)
}
const rendered = computed(() => markdown.render(props.content))
</script>

<template><div class="markdown-preview" v-html="rendered"></div></template>
