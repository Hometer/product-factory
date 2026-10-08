import PlanningEditor from "./planning-editor";

export default async function PlanningPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <PlanningEditor key={id} projectId={id}/>;
}
