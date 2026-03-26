<?php
ini_set('display_errors', 1);
ini_set('display_startup_errors', 1);
error_reporting(E_ALL);

$home = getenv('HOME') ?: posix_getpwuid(posix_getuid())['dir'];
$db_path = $home . '/.price/data/price.db';
$db = new PDO("sqlite:$db_path");
$db->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);

function query($query, $data = null) {
    global $db;
    if ($data == null) {
        $res = $db->query($query);
    } else {
        $res = $db->prepare($query);
        if ($res) {
            if (!$res->execute($data)) {
                throw new Exception("execute query failed");
            }
        } else {
            throw new Exception("wrong query");
        }
    }
    if ($res) {
        if (preg_match("/^\s*INSERT|UPDATE|DELETE|ALTER|CREATE|DROP/i", $query)) {
            return $res->rowCount();
        } else {
            return $res->fetchAll(PDO::FETCH_ASSOC);
        }
    } else {
        throw new Exception("Couldn't open file");
    }
}

function data($date, $name) {
    $query = "SELECT
             p.price,
             datetime(p.timestamp, 'unixepoch', 'utc') as date,
             s.name
           FROM
             price p
           JOIN product pr ON p.product_id = pr.id
           JOIN shop s ON s.id = p.shop_id
           WHERE pr.name = ? AND p.timestamp >= strftime('%s', ?)
           ORDER BY p.timestamp";
    return map(query($query, array($name, $date)));
}

function map($data) {
    $prices = array();
    foreach ($data as $row) {
        $shop = $row['name'];
        $prices[$shop][] = array($row['price'], $row['date']);
    }
    return $prices;
}

if (!isset($_GET['name'])) {
    $date = isset($_GET['date']) ? $_GET['date'] : '2024-01-01';
?><!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Price Tracker</title>
<style>
body { font-family: sans-serif; margin: 2em; }
h1 { margin-bottom: 0.5em; }
ul { list-style: none; padding: 0; }
li { margin: 0.5em 0; }
a { color: #3366E6; text-decoration: none; }
a:hover { text-decoration: underline; }
.website { color: #999; font-size: 0.85em; }
</style>
</head>
<body>
<h1>Price Tracker</h1>
<p>Select a product to view price history:</p>
<ul><?php
$self = $_SERVER['PHP_SELF'];
$rows = query("SELECT pr.name, w.name as website
               FROM product pr
               JOIN website w ON pr.website_id = w.id
               ORDER BY w.name, pr.name");
foreach ($rows as $row) {
    $product = $row['name'];
    $website = htmlspecialchars($row['website']);
    $name = urlencode($product);
    $display = htmlspecialchars($product);
    echo "<li><a href=\"$self?name=$name&date=$date\">$display</a> <span class=\"website\">($website)</span></li>\n";
}
?>
</ul>
</body>
</html><?php
} elseif (!isset($_GET['type'])) {
    $product = htmlspecialchars($_GET['name']);
?>
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title><?= $product ?> — Price History</title>
<style>
body { font-family: sans-serif; margin: 2em; }
h1 { font-size: 1.3em; margin-bottom: 0.3em; }
.back { margin-bottom: 1em; display: inline-block; }
.chart-container { position: relative; width: 100%; height: 500px; }
</style>
</head>
<body>
<a class="back" href="<?= $_SERVER['PHP_SELF'] ?>">&larr; All products</a>
<h1><?= $product ?></h1>
<div class="chart-container"><canvas></canvas></div>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<script>
function format(data) {
    const result = [];
    const colorArray = [
        '#FF6633', '#FFB399', '#FF33FF', '#FFFF99', '#00B3E6',
        '#E6B333', '#3366E6', '#999966', '#99FF99', '#B34D4D',
        '#80B300', '#809900', '#E6B3B3', '#6680B3', '#66991A',
        '#FF99E6', '#CCFF1A', '#FF1A66', '#E6331A', '#33FFCC',
        '#66994D', '#B366CC', '#4D8000', '#B33300', '#CC80CC',
        '#66664D', '#991AFF', '#E666FF', '#4DB3FF', '#1AB399',
        '#E666B3', '#33991A', '#CC9999', '#B3B31A', '#00E680',
        '#4D8066', '#809980', '#E6FF80', '#1AFF33', '#999933',
        '#FF3380', '#CCCC00', '#66E64D', '#4D80CC', '#9900B3',
        '#E64D66', '#4DB380', '#FF4D4D', '#99E6E6', '#6666FF'
    ];
    let i = 0;
    Object.entries(data).forEach(([key, value]) => {
        result.push({
            label: key,
            fill: false,
            borderColor: colorArray[i++ % colorArray.length],
            showLine: true,
            stepped: true,
            pointRadius: 2,
            data: value.map(d => ({y: parseFloat(d[0]), x: new Date(d[1] + 'Z')}))
        });
    });
    return result;
}

fetch(location.href + '&type=json')
  .then(res => res.json())
  .then(data => {
      data = format(data);
      new Chart(document.querySelector('canvas'), {
          type: 'scatter',
          data: { datasets: data },
          options: {
              responsive: true,
              maintainAspectRatio: false,
              scales: {
                  x: {
                      type: 'time',
                      time: {
                          unit: 'day',
                          tooltipFormat: 'MMM dd, yyyy'
                      }
                  },
                  y: {
                      title: {
                          display: true,
                          text: 'Price (zł)'
                      }
                  }
              },
              plugins: {
                  tooltip: {
                      mode: 'point',
                      callbacks: {
                          title: (items) => items.map(i => i.dataset.label)
                      }
                  }
              }
          }
      });
  });
</script>
</body>
</html><?php
} else {
    header('Content-Type: application/json');
    echo json_encode(data($_GET['date'], $_GET['name']));
}
