const CACHE_NAME='mg-business-shell-v2';
const CORE=['./','./index.html','./styles.css','./app.js','./manifest.webmanifest'];
self.addEventListener('install',event=>event.waitUntil(caches.open(CACHE_NAME).then(cache=>cache.addAll(CORE)).then(()=>self.skipWaiting())));
self.addEventListener('activate',event=>event.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(key=>key.startsWith('mg-business-shell-')&&key!==CACHE_NAME).map(key=>caches.delete(key)))).then(()=>self.clients.claim())));
self.addEventListener('fetch',event=>{
 const request=event.request;
 if(request.method!=='GET')return;
 const url=new URL(request.url);
 if(url.origin!==self.location.origin)return;
 event.respondWith((async()=>{
  try{
   const response=await fetch(request,{cache:'no-store'});
   if(response.ok){const cache=await caches.open(CACHE_NAME);await cache.put(request,response.clone());}
   return response;
  }catch(error){
   const cached=await caches.match(request);
   if(cached)return cached;
   if(request.mode==='navigate')return (await caches.match('./index.html'))||Response.error();
   return Response.error();
  }
 })());
});
