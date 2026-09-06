import { LangChainTracer } from "@langchain/core/tracers/tracer_langchain";
import { Client } from "langsmith";

/**
 * Minimal LangSmith setup for the lesson and the report.
 * The tracer hides inputs and outputs by default: the gate's evidence is in
 * the step names and tags, so the trace stays useful with no data in it.
 */
export function traceClient(apiKey: string, options: { hideData?: boolean } = {}) {
  const hide = options.hideData ?? true;
  return new Client({
    apiKey,
    apiUrl: process.env.LANGSMITH_ENDPOINT ?? "https://api.smith.langchain.com",
    ...(hide ? { hideInputs: true, hideOutputs: true } : {}),
  });
}

export function createTracer(client: Client, projectName: string) {
  return new LangChainTracer({ client, projectName });
}
