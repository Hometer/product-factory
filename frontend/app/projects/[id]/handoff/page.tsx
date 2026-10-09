import HandoffPanel from './handoff-panel';
export default async function Page({params}:{params:Promise<{id:string}>}) {
  return <HandoffPanel projectId={(await params).id}/>;
}
