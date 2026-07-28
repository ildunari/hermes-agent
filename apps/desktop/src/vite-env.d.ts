/// <reference types="vite/client" />

declare module 'mammoth/mammoth.browser' {
  interface ConvertResult {
    messages: Array<{ message: string; type: string }>
    value: string
  }

  const mammoth: {
    convertToHtml(input: { arrayBuffer: ArrayBuffer }): Promise<ConvertResult>
  }

  export default mammoth
}
