import ExecutionEditor from "./execution-editor";
export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  return <ExecutionEditor projectId={(await params).id} />;
}
