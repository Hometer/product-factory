import CostsPanel from './costs-panel';
export default async function Page({params}:{params:Promise<{id:string}>}){
  return <CostsPanel projectId={(await params).id}/>;
}
