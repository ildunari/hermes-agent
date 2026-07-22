export interface BrowserUploadMainCopy {
  bytes: string
  cancel: string
  finish: string
  next: string
  select: string
  skip: string
  sourceStagingWarning: string
  sourceTitle: string
}

export const BROWSER_UPLOAD_MAIN_COPY: Record<'en' | 'ja' | 'zh' | 'zh-hant', BrowserUploadMainCopy> = {
  en: {
    bytes: 'bytes', cancel: 'Cancel', finish: 'Finish', next: 'Next', select: 'Select', skip: 'Skip', sourceTitle: 'Select Studio upload source',
    sourceStagingWarning: 'Selecting this source permits temporary staging on this Mac only. Assignment to the website requires a separate approval. Submission or another destructive website effect requires its own later approval.'
  },
  ja: {
    bytes: 'バイト', cancel: 'キャンセル', finish: '完了', next: '次へ', select: '選択', skip: 'スキップ', sourceTitle: 'Studio のアップロード元を選択',
    sourceStagingWarning: 'この選択で許可されるのは、この Mac 上での一時的なステージングだけです。ウェブサイトへの割り当てには別の承認が必要です。送信やその他の破壊的なサイト操作には、その後に個別の承認が必要です。'
  },
  zh: {
    bytes: '字节', cancel: '取消', finish: '完成', next: '下一个', select: '选择', skip: '跳过', sourceTitle: '选择 Studio 上传来源',
    sourceStagingWarning: '选择此来源仅允许在这台 Mac 上临时暂存。分配给网站需要单独批准。提交或其他破坏性网站效果需要之后再次单独批准。'
  },
  'zh-hant': {
    bytes: '位元組', cancel: '取消', finish: '完成', next: '下一個', select: '選取', skip: '略過', sourceTitle: '選取 Studio 上傳來源',
    sourceStagingWarning: '選取此來源僅允許在這台 Mac 上暫時暫存。指派給網站需要個別核准。提交或其他破壞性網站效果之後仍需要另行核准。'
  }
}
