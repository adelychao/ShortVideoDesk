<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { CircleCloseFilled } from '@element-plus/icons-vue'
import { ElButton, ElIcon, ElInput, ElOption, ElPopover, ElSelect } from 'element-plus'
import { ApiError, api } from '../api/client'
import type { DouyinCreatorTag, DouyinCreatorTagCategory, DouyinCreatorTagSearchCategory, DouyinHotTopic, DouyinMetadataDraft, VideoPublishPoi, VideoPublishPoiSearchResult } from '../api/types'

const props = defineProps<{
  accountId: string
  accountName: string
  modelValue: DouyinMetadataDraft
  disabled: boolean
}>()
const emit = defineEmits<{ 'update:modelValue': [value: DouyinMetadataDraft] }>()
const HOT_TOPIC_CACHE_TTL_MS = 3 * 60 * 1000
const POI_CACHE_TTL_MS = 3 * 60 * 1000
const POI_CACHE_MAX_ENTRIES = 50

interface HotTopicCacheEntry {
  items: DouyinHotTopic[]
  fetchedAt: number
}

interface PoiSearchCacheEntry {
  result: VideoPublishPoiSearchResult
  fetchedAt: number
}

interface TagSearchRequest {
  version: number
  accountId: string
  category: DouyinCreatorTagCategory
  keyword: string
  cityCode: string | null
}

const categories: Array<{ value: DouyinCreatorTagCategory; label: string }> = [
  { value: 'location', label: '位置' },
  { value: 'media_entertainment', label: '影视演艺' },
  { value: 'mini_program', label: '小程序' },
  { value: 'game_handle', label: '游戏手柄' },
  { value: 'mark_anything', label: '标记万物' },
]
const category = computed({
  get: () => props.modelValue.category || 'location',
  set: (value: DouyinCreatorTagCategory) => update({ ...props.modelValue, category: value }),
})
const query = computed(() => props.modelValue.queries[category.value] || '')
const setQuery = (value: string) => update({ ...props.modelValue, queries: { ...props.modelValue.queries, [category.value]: value } })
const tagResults = ref<DouyinCreatorTag[]>([])
const poiResults = ref<VideoPublishPoi[]>([])
const poiTabs = ref<VideoPublishPoiSearchResult['tabs']>([])
const cityCode = ref<string | null>(null)
const tagBusy = ref(false)
const tagError = ref('')
const tagSearched = ref(false)
const locationFocused = ref(false)
const locationInput = ref<HTMLInputElement | null>(null)
const hotResults = ref<DouyinHotTopic[]>([])
const hotBusy = ref(false)
const hotError = ref('')
const hotSearched = ref(false)
const hotFocused = ref(false)
const hotInput = ref<HTMLInputElement | null>(null)
const poiSearchCache = new Map<string, PoiSearchCacheEntry>()
const hotTopicCache = new Map<string, HotTopicCacheEntry>()
let tagTimer: number | undefined
let locationBlurTimer: number | undefined
let hotTimer: number | undefined
let hotBlurTimer: number | undefined
let tagVersion = 0
let tagSearchRunning = false
let activeTagSearchKey = ''
let activeTagSearchVersion = 0
let queuedTagSearch: TagSearchRequest | null = null
let hotVersion = 0
let activeHotQuery = ''
let recommendationRequest: Promise<DouyinHotTopic[]> | undefined

function update(value: DouyinMetadataDraft): void {
  emit('update:modelValue', {
    category: value.category,
    queries: { ...value.queries },
    creator_tags: value.creator_tags.map(tag => ({ ...tag, mount_options: tag.mount_options?.map(option => ({ ...option })) })),
    hot_topic_query: value.hot_topic_query,
    hot_topic: value.hot_topic ? { ...value.hot_topic } : null,
  })
}

function categoryName(value: DouyinCreatorTagCategory): string {
  return categories.find(item => item.value === value)?.label || '标签'
}

function selectedTag(value = category.value): DouyinCreatorTag | undefined {
  return props.modelValue.creator_tags.find(tag => tag.category === value)
}

const locationInputValue = computed(() => selectedTag('location')?.name || (category.value === 'location' ? query.value : ''))
const locationPopoverWidth = computed(() => Math.round(locationInput.value?.getBoundingClientRect().width || 480))

function safeTag(tag: DouyinCreatorTag): DouyinCreatorTag {
  const clean: DouyinCreatorTag = { id: tag.id, category: tag.category, name: tag.name }
  if (tag.category === 'location') {
    clean.address = tag.address || ''
    clean.city_code = tag.city_code || ''
    clean.city_name = tag.city_name || ''
    clean.search_tab_code = tag.search_tab_code
  }
  if (tag.category === 'mini_program') clean.share_url = tag.share_url || query.value
  if (tag.category === 'game_handle') {
    const option = tag.mount_options?.[0]
    if (tag.mount_option_id) {
      clean.mount_option_id = tag.mount_option_id
      clean.mount_option_description = tag.mount_option_description || tag.mount_options?.find(item => item.id === tag.mount_option_id)?.description
    } else if (option) {
      clean.mount_option_id = option.id
      clean.mount_option_description = option.description
    }
  }
  return clean
}

function selectTag(tag: DouyinCreatorTag): void {
  const value = safeTag(tag)
  update({ ...props.modelValue, creator_tags: [...props.modelValue.creator_tags.filter(item => item.category !== value.category), value] })
  if (value.category === 'location') locationFocused.value = false
  tagResults.value = []
  poiResults.value = []
  tagError.value = ''
}

function clonePoiSearchResult(result: VideoPublishPoiSearchResult): VideoPublishPoiSearchResult {
  return {
    items: result.items.map(item => ({ ...item })),
    tabs: result.tabs.map(tab => ({ ...tab })),
    cities: result.cities.map(city => ({ ...city })),
    selected_city_code: result.selected_city_code,
  }
}

function readPoiSearchCache(key: string): VideoPublishPoiSearchResult | undefined {
  const entry = poiSearchCache.get(key)
  if (!entry) return undefined
  if (Date.now() - entry.fetchedAt > POI_CACHE_TTL_MS) {
    poiSearchCache.delete(key)
    return undefined
  }
  poiSearchCache.delete(key)
  poiSearchCache.set(key, entry)
  return clonePoiSearchResult(entry.result)
}

function writePoiSearchCache(key: string, result: VideoPublishPoiSearchResult): void {
  poiSearchCache.delete(key)
  poiSearchCache.set(key, { result: clonePoiSearchResult(result), fetchedAt: Date.now() })
  if (poiSearchCache.size > POI_CACHE_MAX_ENTRIES) {
    const oldestKey = poiSearchCache.keys().next().value
    if (oldestKey !== undefined) poiSearchCache.delete(oldestKey)
  }
}

function showPoiSearchResult(result: VideoPublishPoiSearchResult): void {
  poiResults.value = result.items.map(item => ({ ...item }))
  poiTabs.value = [...result.tabs, ...result.cities]
  cityCode.value = result.selected_city_code
  tagSearched.value = true
}

function clearTag(tagCategory: DouyinCreatorTagCategory): void {
  const queries = { ...props.modelValue.queries }
  if (tagCategory === category.value) queries[tagCategory] = ''
  update({ ...props.modelValue, creator_tags: props.modelValue.creator_tags.filter(item => item.category !== tagCategory), queries })
  tagResults.value = []
  poiResults.value = []
  tagSearched.value = false
}

async function runTagSearch(searchText = query.value): Promise<void> {
  if (tagTimer !== undefined) window.clearTimeout(tagTimer)
  const keyword = searchText.trim()
  const searchCategory = category.value
  if (!keyword && searchCategory !== 'location') return
  const request: TagSearchRequest = {
    version: tagVersion + 1,
    accountId: props.accountId,
    category: searchCategory,
    keyword,
    cityCode: searchCategory === 'location' ? cityCode.value || null : null,
  }
  const requestKey = tagSearchKey(request)
  const cachedPoiResult = searchCategory === 'location' ? readPoiSearchCache(requestKey) : undefined
  if (cachedPoiResult) {
    tagVersion = request.version
    queuedTagSearch = null
    tagBusy.value = false
    tagError.value = ''
    tagResults.value = []
    showPoiSearchResult(cachedPoiResult)
    return
  }
  if (tagSearchRunning && (
    (activeTagSearchKey === requestKey && activeTagSearchVersion === tagVersion)
    || (queuedTagSearch && tagSearchKey(queuedTagSearch) === requestKey && queuedTagSearch.version === tagVersion)
  )) return

  tagVersion = request.version
  tagBusy.value = true
  tagError.value = ''
  tagResults.value = []
  poiResults.value = []

  if (tagSearchRunning) {
    queuedTagSearch = request
    return
  }

  tagSearchRunning = true
  let currentRequest: TagSearchRequest | null = request
  try {
    while (currentRequest) {
      const activeRequest: TagSearchRequest = currentRequest
      activeTagSearchKey = tagSearchKey(activeRequest)
      activeTagSearchVersion = activeRequest.version
      if (activeRequest.version === tagVersion) {
        try {
          if (activeRequest.category === 'location') {
            const response = await api.searchDouyinPoi(activeRequest.accountId, activeRequest.keyword, activeRequest.cityCode)
            writePoiSearchCache(tagSearchKey(activeRequest), response)
            if (activeRequest.cityCode === null) {
              writePoiSearchCache(tagSearchKey({ ...activeRequest, cityCode: response.selected_city_code }), response)
            }
            if (activeRequest.version === tagVersion) {
              showPoiSearchResult(response)
            }
          } else {
            const response = await api.searchDouyinCreatorTags(activeRequest.accountId, activeRequest.category as DouyinCreatorTagSearchCategory, activeRequest.keyword)
            if (activeRequest.version === tagVersion) {
              tagResults.value = response.items
              tagSearched.value = true
            }
          }
        } catch (error) {
          if (activeRequest.version === tagVersion) {
            tagError.value = error instanceof ApiError
              ? error.message
              : '抖音搜索暂不可用，请检查账号连接后重试。'
          }
        }
      }

      const queuedRequest = queuedTagSearch
      queuedTagSearch = null
      currentRequest = queuedRequest && queuedRequest.version === tagVersion ? queuedRequest : null
    }
  } finally {
    tagSearchRunning = false
    activeTagSearchKey = ''
    activeTagSearchVersion = 0
    queuedTagSearch = null
    tagBusy.value = false
  }
}

function tagSearchKey(request: Pick<TagSearchRequest, 'accountId' | 'category' | 'keyword' | 'cityCode'>): string {
  return JSON.stringify([request.accountId, request.category, request.keyword, request.cityCode])
}

function updateTagQuery(value: string): void {
  setQuery(value)
  queueTagSearch(value)
}

function queueTagSearch(value = query.value): void {
  if (tagTimer !== undefined) window.clearTimeout(tagTimer)
  tagVersion++
  queuedTagSearch = null
  tagBusy.value = false
  tagError.value = ''
  tagSearched.value = false
  tagResults.value = []
  poiResults.value = []
  const keyword = value.trim()
  if ((!keyword && category.value !== 'location') || selectedTag()) return
  tagTimer = window.setTimeout(() => { void runTagSearch(keyword) }, category.value === 'location' ? 300 : 500)
}

function choosePoi(poi: VideoPublishPoi): void {
  selectTag({ ...poi, category: 'location' })
}

function setLocationInputRef(element: unknown): void {
  const root = (element as { $el?: HTMLElement } | null)?.$el
  locationInput.value = root?.querySelector('input') ?? null
}

function onLocationFocus(): void {
  if (locationBlurTimer !== undefined) window.clearTimeout(locationBlurTimer)
  locationFocused.value = true
  if (selectedTag('location')) return
  if (tagTimer !== undefined) window.clearTimeout(tagTimer)
  const keyword = query.value.trim()
  if (keyword || readPoiSearchCache(tagSearchKey({
    accountId: props.accountId, category: 'location', keyword, cityCode: cityCode.value,
  }))) {
    void runTagSearch(keyword)
    return
  }
  tagTimer = window.setTimeout(() => { void runTagSearch() }, 250)
}

function onLocationBlur(): void {
  if (locationBlurTimer !== undefined) window.clearTimeout(locationBlurTimer)
  locationBlurTimer = window.setTimeout(() => { locationFocused.value = false }, 120)
}

function clearLocationInput(): void {
  if (tagTimer !== undefined) window.clearTimeout(tagTimer)
  tagVersion++
  if (selectedTag('location')) clearTag('location')
  else setQuery('')
  locationFocused.value = true
  void runTagSearch('')
}

function closeLocationDropdown(): void {
  locationFocused.value = false
}

function poiDistanceLabel(distance?: string): string {
  const value = distance?.trim() || ''
  return /^\d+(?:\.\d+)?$/.test(value) ? `${value}m` : value
}

function changeCity(code: string): void {
  if (cityCode.value === code) return
  cityCode.value = code
  void runTagSearch()
}

const hotInputValue = computed(() => props.modelValue.hot_topic?.title || props.modelValue.hot_topic_query)
const hotPopoverWidth = computed(() => Math.round(hotInput.value?.getBoundingClientRect().width || 480))

function hotCacheKey(keyword: string): string {
  return keyword.trim().toLocaleLowerCase()
}

function readHotCache(key: string): HotTopicCacheEntry | undefined {
  const entry = hotTopicCache.get(key)
  if (!entry) return undefined
  if (Date.now() - entry.fetchedAt > HOT_TOPIC_CACHE_TTL_MS) {
    hotTopicCache.delete(key)
    return undefined
  }
  hotTopicCache.delete(key)
  hotTopicCache.set(key, entry)
  return entry
}

function writeHotCache(key: string, items: DouyinHotTopic[]): void {
  hotTopicCache.delete(key)
  hotTopicCache.set(key, { items: items.map(item => ({ ...item })), fetchedAt: Date.now() })
  if (hotTopicCache.size > 20) {
    const oldestKey = hotTopicCache.keys().next().value
    if (oldestKey !== undefined) hotTopicCache.delete(oldestKey)
  }
}

function loadHotRecommendations(): Promise<DouyinHotTopic[]> {
  const cached = readHotCache('')
  if (cached) return Promise.resolve(cached.items)
  if (recommendationRequest) return recommendationRequest
  const request = api.searchDouyinHotTopics(props.accountId).then(({ items }) => {
    if (recommendationRequest === request) writeHotCache('', items)
    return items
  }).finally(() => {
    if (recommendationRequest === request) recommendationRequest = undefined
  })
  recommendationRequest = request
  return request
}

function matchingHotRecommendations(key: string): DouyinHotTopic[] {
  return key ? (readHotCache('')?.items || []).filter(item =>
    hotCacheKey(item.title).includes(key)) : []
}

function setHotInputRef(element: unknown): void {
  const root = (element as { $el?: HTMLElement } | null)?.$el
  hotInput.value = root?.querySelector('input') ?? null
}

function setHotQuery(value: string): void {
  update({ ...props.modelValue, hot_topic: null, hot_topic_query: value })
  if (hotTimer !== undefined) window.clearTimeout(hotTimer)
  hotVersion++
  activeHotQuery = ''
  hotBusy.value = false
  hotError.value = ''
  hotResults.value = matchingHotRecommendations(hotCacheKey(value))
  hotSearched.value = false
  if (value.trim()) hotTimer = window.setTimeout(() => { void runHotSearch(value.trim()) }, 300)
  else if (hotFocused.value) hotTimer = window.setTimeout(() => { void runHotSearch('') }, 200)
}

async function runHotSearch(keyword = props.modelValue.hot_topic_query.trim()): Promise<void> {
  const cacheKey = hotCacheKey(keyword)
  const cached = readHotCache(cacheKey)
  if (cached) {
    hotVersion++
    activeHotQuery = ''
    hotBusy.value = false
    hotError.value = ''
    hotResults.value = cached.items.map(item => ({ ...item }))
    hotSearched.value = true
    return
  }
  if (hotBusy.value && activeHotQuery === cacheKey) return
  const version = ++hotVersion
  activeHotQuery = cacheKey
  hotBusy.value = true
  hotError.value = ''
  hotResults.value = matchingHotRecommendations(cacheKey)
  try {
    const items = keyword
      ? (await api.searchDouyinHotTopics(props.accountId, keyword)).items
      : await loadHotRecommendations()
    if (version !== hotVersion) return
    const merged = new Map(items.map(item => [item.id, item]))
    for (const item of matchingHotRecommendations(cacheKey)) {
      if (!merged.has(item.id)) merged.set(item.id, item)
    }
    hotResults.value = [...merged.values()]
    writeHotCache(cacheKey, hotResults.value)
    hotSearched.value = true
  } catch (error) {
    if (version === hotVersion) {
      hotError.value = error instanceof ApiError ? error.message : '热点列表暂不可用，请稍后重试。'
    }
  } finally {
    if (version === hotVersion) {
      hotBusy.value = false
      activeHotQuery = ''
    }
  }
}

function selectHotTopic(topic: DouyinHotTopic): void {
  if (hotTimer !== undefined) window.clearTimeout(hotTimer)
  hotVersion++
  hotBusy.value = false
  activeHotQuery = ''
  update({ ...props.modelValue, hot_topic_query: topic.title, hot_topic: { id: topic.id, title: topic.title } })
  hotFocused.value = false
  hotResults.value = []
  hotError.value = ''
}

function clearHotInput(): void {
  if (hotTimer !== undefined) window.clearTimeout(hotTimer)
  hotVersion++
  activeHotQuery = ''
  hotBusy.value = false
  update({ ...props.modelValue, hot_topic: null, hot_topic_query: '' })
  hotFocused.value = true
  hotResults.value = []
  hotError.value = ''
  hotSearched.value = false
  void runHotSearch('')
}

function onHotFocus(): void {
  if (hotBlurTimer !== undefined) window.clearTimeout(hotBlurTimer)
  hotFocused.value = true
  void runHotSearch(props.modelValue.hot_topic?.title || props.modelValue.hot_topic_query.trim())
}

function onHotBlur(): void {
  if (hotBlurTimer !== undefined) window.clearTimeout(hotBlurTimer)
  hotBlurTimer = window.setTimeout(() => { hotFocused.value = false }, 120)
}

function closeHotDropdown(): void {
  hotFocused.value = false
}

watch(() => [props.accountId, category.value], () => {
  if (tagTimer !== undefined) window.clearTimeout(tagTimer)
  tagVersion++
  queuedTagSearch = null
  tagBusy.value = false
  tagResults.value = []
  poiResults.value = []
  poiTabs.value = []
  cityCode.value = null
  locationFocused.value = false
  tagError.value = ''
  tagSearched.value = false
})
watch(() => props.accountId, () => {
  hotVersion++
  activeHotQuery = ''
  hotBusy.value = false
  hotResults.value = []
  hotError.value = ''
  hotSearched.value = false
  hotTopicCache.clear()
  recommendationRequest = undefined
  void loadHotRecommendations().catch(() => undefined)
}, { immediate: true })
onBeforeUnmount(() => {
  tagVersion++
  queuedTagSearch = null
  hotVersion++
  recommendationRequest = undefined
  if (tagTimer !== undefined) window.clearTimeout(tagTimer)
  if (locationBlurTimer !== undefined) window.clearTimeout(locationBlurTimer)
  if (hotTimer !== undefined) window.clearTimeout(hotTimer)
  if (hotBlurTimer !== undefined) window.clearTimeout(hotBlurTimer)
})
</script>

<template>
  <div class="douyin-metadata-editor">
    <div class="metadata-row">
      <span class="metadata-label">添加标签</span>
      <div class="metadata-main">
        <div class="tag-search-row">
          <ElSelect v-model="category" class="category-select" :disabled="disabled" :aria-label="`${accountName} 标签类型`">
            <ElOption v-for="item in categories" :key="item.value" :label="item.label" :value="item.value" />
          </ElSelect>
          <div v-if="category === 'location'" class="location-input-wrap">
            <ElInput :ref="setLocationInputRef" :model-value="locationInputValue" :disabled="disabled || !!selectedTag('location')" maxlength="100" :aria-label="`${accountName} 地理位置搜索`" placeholder="输入地理位置" @focus="onLocationFocus" @blur="onLocationBlur" @update:model-value="updateTagQuery" @keydown.esc="closeLocationDropdown">
              <template #suffix>
                <button class="location-clear-button" type="button" :disabled="disabled" aria-label="清除位置" @mousedown.prevent @click.stop="clearLocationInput">
                  <ElIcon><CircleCloseFilled /></ElIcon>
                </button>
              </template>
            </ElInput>
            <ElPopover :visible="locationFocused && !selectedTag('location')" :virtual-ref="locationInput || undefined" virtual-triggering placement="bottom-start" :fallback-placements="['top-start', 'bottom-end', 'top-end']" :width="locationPopoverWidth" :show-arrow="false" popper-class="douyin-location-popper" :popper-style="{ padding: '0' }">
              <div class="poi-dropdown" aria-label="抖音地点搜索结果">
                <div v-if="poiTabs.length" class="poi-tabs" role="tablist" aria-label="地点范围">
                  <button v-for="tab in poiTabs" :key="tab.code" class="poi-tab" type="button" role="tab" :class="{ active: tab.code === cityCode }" :aria-selected="tab.code === cityCode" @mousedown.prevent @click="changeCity(tab.code)">{{ tab.name }}</button>
                </div>
                <div class="poi-dropdown-list" role="listbox">
                  <small v-if="tagBusy" class="poi-dropdown-message" role="status">正在查询抖音地点…</small>
                  <small v-if="tagError" class="poi-dropdown-message poi-dropdown-error" role="alert">{{ tagError }}</small>
                  <button v-if="tagError" class="poi-retry-button" type="button" :disabled="tagBusy" @mousedown.prevent @click="runTagSearch()">重试</button>
                  <button v-for="poi in poiResults" :key="poi.id" class="poi-result" type="button" role="option" @mousedown.prevent @click="choosePoi(poi)">
                    <span class="poi-result-copy"><strong>{{ poi.name }}</strong><small>{{ poi.address }}</small></span>
                    <small v-if="poi.distance" class="poi-distance">{{ poiDistanceLabel(poi.distance) }}</small>
                  </button>
                  <small v-if="tagSearched && !tagBusy && !tagError && !poiResults.length" class="poi-dropdown-message">没有找到地点，请换个关键词或切换范围。</small>
                </div>
              </div>
            </ElPopover>
          </div>
          <template v-else>
            <ElInput :model-value="query" :disabled="disabled || !!selectedTag()" maxlength="2048" :aria-label="`${accountName} ${categoryName(category)}搜索`" :placeholder="category === 'mini_program' ? '粘贴抖音小程序分享链接' : category === 'game_handle' ? '输入游戏名称搜索' : `输入${categoryName(category)}名称搜索`" @update:model-value="updateTagQuery" @keyup.enter="() => runTagSearch()" />
            <ElButton native-type="button" :disabled="disabled || tagBusy || !query.trim() || !!selectedTag()" @click="() => runTagSearch()">搜索</ElButton>
          </template>
        </div>
        <div v-if="category !== 'location' && !selectedTag() && (tagBusy || tagError || tagResults.length || tagSearched)" class="metadata-results" role="listbox" :aria-label="`${categoryName(category)}搜索结果`">
          <small v-if="tagBusy" role="status">正在查询抖音 {{ categoryName(category) }}…</small>
          <small v-if="tagError" class="metadata-error" role="alert">{{ tagError }}</small>
          <button v-for="tag in tagResults" :key="tag.id" class="metadata-result" type="button" @click="selectTag(tag)">
            <img v-if="tag.thumbnail_url" :src="tag.thumbnail_url" alt="" loading="lazy" referrerpolicy="no-referrer" />
            <span><strong>{{ tag.name }}</strong><small>{{ tag.detail || (tag.mount_options?.[0]?.description ?? '') }}</small></span>
          </button>
          <small v-if="tagSearched && !tagBusy && !tagError && !tagResults.length">未找到可确认的结果，请调整关键词后重试。</small>
        </div>
        <div v-if="selectedTag() && category !== 'location'" class="metadata-selected">
          <span><b>{{ categoryName(category) }}</b>：{{ selectedTag()?.name }}<small v-if="selectedTag()?.address"> · {{ selectedTag()?.address }}</small><small v-if="selectedTag()?.mount_option_description"> · {{ selectedTag()?.mount_option_description }}</small></span>
          <ElButton text native-type="button" :disabled="disabled" @click="clearTag(category)">清除</ElButton>
        </div>
        <div v-if="props.modelValue.creator_tags.some(tag => tag.category !== category)" class="metadata-chips" aria-label="已添加标签">
          <span v-for="tag in props.modelValue.creator_tags.filter(item => item.category !== category)" :key="tag.category">{{ categoryName(tag.category) }}：{{ tag.name }}<button type="button" :disabled="disabled" :aria-label="`清除${categoryName(tag.category)}`" @click="clearTag(tag.category)">×</button></span>
        </div>
        <small class="metadata-hint">从抖音创作者中心搜索并选择平台结果。位置、官方标签与普通 #话题分别保存。</small>
      </div>
    </div>
    <div class="metadata-row hot-row">
      <span class="metadata-label">关联热点</span>
      <div class="metadata-main hot-main">
        <div class="hot-input-wrap">
          <ElInput :ref="setHotInputRef" :model-value="hotInputValue" :disabled="disabled" maxlength="100" placeholder="点击输入热点词" :aria-label="`${accountName} 关联热点搜索`" @focus="onHotFocus" @blur="onHotBlur" @update:model-value="setHotQuery" @keydown.esc="closeHotDropdown">
            <template #suffix>
              <button class="hot-clear-button" type="button" :disabled="disabled" aria-label="清除关联热点" @mousedown.prevent @click.stop="clearHotInput">
                <ElIcon><CircleCloseFilled /></ElIcon>
              </button>
            </template>
          </ElInput>
          <ElPopover :visible="hotFocused" :virtual-ref="hotInput || undefined" virtual-triggering placement="bottom-start" :fallback-placements="['top-start', 'bottom-end', 'top-end']" :width="hotPopoverWidth" :show-arrow="false" popper-class="douyin-hot-topic-popper" :popper-style="{ maxHeight: '360px', padding: '0' }">
            <div class="hot-topic-dropdown" role="listbox" aria-label="抖音关联热点搜索结果">
              <strong class="hot-dropdown-heading">{{ props.modelValue.hot_topic_query.trim() ? '热点搜索' : '全部热点' }}</strong>
              <small v-if="hotBusy" class="hot-dropdown-message" role="status">正在查询抖音热点…</small>
              <small v-if="hotError" class="hot-dropdown-message hot-dropdown-error" role="alert">{{ hotError }}</small>
              <button v-if="hotError" class="hot-retry-button" type="button" :disabled="hotBusy" @mousedown.prevent @click="runHotSearch()">重试</button>
              <button v-for="topic in hotResults" :key="topic.id" class="hot-dropdown-result" type="button" role="option" :aria-selected="props.modelValue.hot_topic?.id === topic.id" @mousedown.prevent @click="selectHotTopic(topic)">
                <img v-if="topic.thumbnail_url" :src="topic.thumbnail_url" alt="" loading="lazy" referrerpolicy="no-referrer" />
                <span class="hot-result-title">{{ topic.title }}</span>
                <small class="hot-result-count">{{ topic.view_count_text }}</small>
              </button>
              <small v-if="hotSearched && !hotBusy && !hotError && !hotResults.length" class="hot-dropdown-message">没有匹配热点，请换关键词试试。</small>
            </div>
          </ElPopover>
        </div>
        <small class="metadata-hint">每个发布账号可选择一个热点；热点独立提交，不会转成普通 #话题。</small>
      </div>
    </div>
  </div>
</template>

<style scoped>
.douyin-metadata-editor { display: grid; gap: 12px; min-width: 0; }
.metadata-row { display: grid; grid-template-columns: 96px minmax(0, 1fr); gap: 12px; align-items: start; min-width: 0; }
.metadata-label { padding-top: 9px; font-size: 14px; font-weight: 600; color: var(--el-text-color-primary, #222); }
.metadata-main { display: grid; gap: 8px; min-width: 0; position: relative; }
.tag-search-row { display: grid; grid-template-columns: 128px minmax(0, 1fr) auto; gap: 8px; min-width: 0; }
.category-select { width: 128px; }
.location-input-wrap { position: relative; min-width: 0; }
.location-clear-button { display: inline-flex; align-items: center; justify-content: center; padding: 0; border: 0; background: transparent; color: var(--text-tertiary); cursor: pointer; }
.location-clear-button:disabled { cursor: not-allowed; opacity: .55; }
.location-clear-button .el-icon { font-size: 16px; }
.poi-dropdown { max-height: 360px; overflow: hidden; min-width: 0; border: 1px solid var(--line); border-radius: 7px; background: var(--paper); color: var(--text-primary); }
.poi-tabs { position: sticky; top: 0; z-index: 1; display: flex; flex-wrap: wrap; align-items: stretch; gap: 0 22px; min-height: 46px; padding: 0 16px; border-bottom: 1px solid var(--line); background: var(--paper); }
.poi-tab { position: relative; display: inline-flex; align-items: center; padding: 0 2px; border: 0; background: transparent; color: var(--text-tertiary); font-size: 13px; cursor: pointer; }
.poi-tab.active { color: var(--text-primary); font-weight: var(--fw-semibold); }
.poi-tab.active::after { position: absolute; right: 0; bottom: 0; left: 0; height: 2px; background: var(--text-primary); content: ''; }
.poi-dropdown-list { max-height: 312px; overflow-y: auto; padding: 4px 0; }
.poi-result { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 12px; width: 100%; min-width: 0; padding: 10px 16px; border: 0; background: transparent; color: inherit; text-align: left; cursor: pointer; }
.poi-result:hover, .poi-result:focus-visible { background: var(--surface-hover); outline: none; }
.poi-result-copy { display: block; min-width: 0; }
.poi-result-copy strong, .poi-result-copy small { display: block; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.poi-result-copy strong { font-size: 13px; font-weight: var(--fw-medium); }
.poi-result-copy small { margin-top: 3px; color: var(--text-tertiary); font-size: 12px; }
.poi-distance { color: var(--text-tertiary); font-size: 12px; white-space: nowrap; font-variant-numeric: tabular-nums; }
.poi-dropdown-message { display: block; padding: 14px 16px; color: var(--text-secondary); font-size: 12px; }
.poi-dropdown-error { color: var(--danger); }
.poi-retry-button { margin: 0 16px 10px; padding: 0; border: 0; background: transparent; color: var(--text-selected); font-size: 12px; cursor: pointer; }
.metadata-results { display: grid; gap: 4px; max-height: 260px; overflow: auto; padding: 8px; border: 1px solid var(--el-border-color, #dcdfe6); border-radius: 8px; background: var(--el-bg-color, #fff); min-width: 0; }
.metadata-result { display: flex; align-items: center; gap: 10px; width: 100%; min-width: 0; padding: 8px; border: 0; border-radius: 6px; background: transparent; color: inherit; text-align: left; cursor: pointer; }
.metadata-result:hover, .metadata-result:focus-visible { background: var(--el-fill-color-light, #f5f7fa); outline: none; }
.metadata-result img { width: 36px; height: 36px; flex: 0 0 36px; border-radius: 5px; object-fit: cover; }
.metadata-result span, .metadata-result strong, .metadata-result small { min-width: 0; display: block; }
.metadata-result strong { overflow-wrap: anywhere; }
.metadata-result small { margin-top: 3px; color: var(--el-text-color-secondary, #737780); }
.metadata-results > small { font-size: 12px; color: var(--el-text-color-secondary, #737780); padding: 6px; }
.metadata-results .metadata-error { color: var(--el-color-danger, #c45656); }
.metadata-selected { display: flex; justify-content: space-between; align-items: center; gap: 8px; min-width: 0; padding: 8px 10px; border-radius: 7px; background: var(--el-fill-color-light, #f5f7fa); font-size: 13px; overflow-wrap: anywhere; }
.metadata-selected small { color: var(--el-text-color-secondary, #737780); }
.metadata-chips { display: flex; flex-wrap: wrap; gap: 6px; }
.metadata-chips > span { display: inline-flex; align-items: center; gap: 5px; max-width: 100%; padding: 4px 8px; border-radius: 16px; background: var(--el-fill-color-light, #f5f7fa); font-size: 12px; overflow-wrap: anywhere; }
.metadata-chips button { border: 0; background: transparent; cursor: pointer; font-size: 16px; }
.metadata-hint { font-size: 12px; color: var(--el-text-color-secondary, #8b9099); line-height: 1.5; }
.hot-main { gap: 8px; }
.hot-input-wrap { position: relative; min-width: 0; }
.hot-clear-button { display: inline-flex; align-items: center; justify-content: center; padding: 0; border: 0; background: transparent; color: var(--text-tertiary); cursor: pointer; }
.hot-clear-button:disabled { cursor: not-allowed; opacity: .55; }
.hot-clear-button .el-icon { font-size: 16px; }
.hot-topic-dropdown { display: grid; gap: 2px; max-height: 340px; overflow: auto; padding: 8px; min-width: 0; border: 1px solid var(--line); border-radius: 8px; background: var(--paper); color: var(--text-primary); }
.hot-dropdown-heading { padding: 5px 8px; font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); color: var(--text-secondary); }
.hot-dropdown-result { display: grid; grid-template-columns: 32px minmax(0, 1fr) auto; align-items: center; gap: 8px; width: 100%; min-width: 0; padding: 6px 8px; border: 0; border-radius: 6px; background: transparent; color: inherit; text-align: left; cursor: pointer; }
.hot-dropdown-result:hover, .hot-dropdown-result:focus-visible { background: var(--surface-hover); outline: none; }
.hot-dropdown-result img { width: 32px; height: 32px; border-radius: 4px; object-fit: cover; }
.hot-result-title { min-width: 0; overflow-wrap: anywhere; font-size: var(--fs-body); color: var(--text-primary); }
.hot-result-count { white-space: nowrap; font-size: var(--fs-body-sm); color: var(--text-tertiary); font-variant-numeric: tabular-nums; }
.hot-dropdown-message { padding: 8px; font-size: var(--fs-body-sm); color: var(--text-secondary); }
.hot-dropdown-error { color: var(--danger); }
.hot-retry-button { justify-self: start; margin: 0 8px 4px; padding: 4px 0; border: 0; background: transparent; color: var(--text-selected); font-size: var(--fs-body-sm); cursor: pointer; }
@media (max-width: 680px) {
  .metadata-row { grid-template-columns: minmax(0, 1fr); gap: 5px; }
  .metadata-label { padding-top: 0; }
  .tag-search-row { grid-template-columns: minmax(0, 1fr) auto; }
  .category-select { grid-column: 1 / -1; width: 100%; }
  .location-input-wrap { grid-column: 1 / -1; }
}
</style>
