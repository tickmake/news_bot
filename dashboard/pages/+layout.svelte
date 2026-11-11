<!-- Evidence's own layout, with the site's name in the header. -->
<script>
	import '@evidence-dev/tailwind/fonts.css';
	import '../app.css';
	import { EvidenceDefaultLayout } from '@evidence-dev/core-components';
	export let data;
</script>

<EvidenceDefaultLayout {data} title="Fjord Signals">
	<slot slot="content" />
</EvidenceDefaultLayout>
