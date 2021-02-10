$(document).ready(function() {

    $(".docker-control").click(function() {
        var $el = $(this);
        var action = $el.data('action')
        var name = $el.data('name');
        $el.text(action + '...');
        $.get("/cicd/instance/" + action + "?name=" + name).then(function(result) {
            document.location.reload();
        });
    });
});
