import TemplatesPanel from './templates-panel';
export default async function Page({params}:{params:Promise<{id:string}>}){
  return <TemplatesPanel projectId={(await params).id}/>;
}
