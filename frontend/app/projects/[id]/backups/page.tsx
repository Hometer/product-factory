import BackupsPanel from './backups-panel';
export default async function Page({params}:{params:Promise<{id:string}>}){
  return <BackupsPanel projectId={(await params).id}/>;
}
