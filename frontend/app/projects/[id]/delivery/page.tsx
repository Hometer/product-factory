import DeliveryPanel from './delivery-panel';
export default async function Page({params}:{params:Promise<{id:string}>}){return <DeliveryPanel projectId={(await params).id}/>;}
